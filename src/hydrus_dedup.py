"""
Hydrus視覚的重複検知（perceptual hash）バッチ後処理モジュール

クロール完了後にHydrusのperceptual hashによる潜在重複ペアを取得し、
古い方を残して新しい方を削除する。タグ/URL/ノートはマージされる。

使用するHydrus API権限: MANAGE_FILE_RELATIONSHIPS (Permission ID 8)
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("EventMonitor.HydrusDedup")


class HydrusDedup:
    """Hydrus perceptual hash重複検知・解消クラス"""

    def __init__(self, config: Dict[str, Any]):
        hydrus_config = config.get("hydrus", {})
        self.enabled = hydrus_config.get("enabled", False)
        self.api_url = hydrus_config.get("api_url", "http://127.0.0.1:45869")

        import os
        self.access_key = os.environ.get("HYDRUS_ACCESS_KEY") or hydrus_config.get("access_key")

        dedup_config = hydrus_config.get("dedup", {})
        self.dedup_enabled = dedup_config.get("enabled", False)
        self.max_hamming_distance = dedup_config.get("max_hamming_distance", 4)
        self.potentials_search_type = dedup_config.get("potentials_search_type", 0)
        self.pixel_duplicates = dedup_config.get("pixel_duplicates", 1)
        self.polling_interval = dedup_config.get("polling_interval", 10)
        self.polling_max_wait = dedup_config.get("polling_max_wait", 300)
        self.dry_run = dedup_config.get("dry_run", False)

        self.session: Optional[aiohttp.ClientSession] = None
        self._session_key: Optional[str] = None

    @property
    def is_active(self) -> bool:
        """dedup機能が有効かつHydrusが有効か"""
        return self.enabled and self.dedup_enabled

    # --- async context manager ---

    async def __aenter__(self):
        if self.is_active:
            self.session = aiohttp.ClientSession()
            await self._get_session_key()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    # --- 内部ヘルパー ---

    async def _get_session_key(self) -> Optional[str]:
        try:
            headers = {"Hydrus-Client-API-Access-Key": self.access_key}
            async with self.session.get(f"{self.api_url}/session_key", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._session_key = data.get("session_key")
                    logger.info("Hydrus Dedup: セッションキーを取得しました")
                    return self._session_key
                else:
                    logger.error(f"Hydrus Dedup: セッションキー取得エラー: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Hydrus Dedup: API接続エラー: {e}")
            return None

    def _get_headers(self) -> Dict[str, str]:
        if self._session_key:
            return {"Hydrus-Client-API-Session-Key": self._session_key}
        return {"Hydrus-Client-API-Access-Key": self.access_key}

    # --- Hydrus API呼び出し ---

    async def get_potentials_count(self) -> int:
        """未処理の潜在重複ペア数を取得"""
        headers = self._get_headers()
        params = {
            "max_hamming_distance": self.max_hamming_distance,
            "potentials_search_type": self.potentials_search_type,
            "pixel_duplicates": self.pixel_duplicates,
        }
        async with self.session.get(
            f"{self.api_url}/manage_file_relationships/get_potentials_count",
            headers=headers,
            params=params,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("potential_duplicates_count", 0)
            else:
                text = await resp.text()
                logger.error(f"get_potentials_count失敗: {resp.status} {text}")
                return 0

    async def get_potential_pairs(self) -> List[List[str]]:
        """潜在重複ペアのハッシュリストを取得"""
        headers = self._get_headers()
        params = {
            "max_hamming_distance": self.max_hamming_distance,
            "potentials_search_type": self.potentials_search_type,
            "pixel_duplicates": self.pixel_duplicates,
        }
        async with self.session.get(
            f"{self.api_url}/manage_file_relationships/get_potential_pairs",
            headers=headers,
            params=params,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("potential_duplicate_pairs", [])
            else:
                text = await resp.text()
                logger.error(f"get_potential_pairs失敗: {resp.status} {text}")
                return []

    async def get_file_metadata(self, file_hash: str) -> Optional[Dict[str, Any]]:
        """ファイルのメタデータを取得（import_time等）"""
        headers = self._get_headers()
        params = {"hash": file_hash}
        async with self.session.get(
            f"{self.api_url}/get_files/file_metadata",
            headers=headers,
            params=params,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                metadata_list = data.get("metadata", [])
                if metadata_list:
                    return metadata_list[0]
            else:
                logger.error(f"get_file_metadata失敗 ({file_hash[:16]}...): {resp.status}")
            return None

    async def set_file_relationship(
        self, hash_a: str, hash_b: str
    ) -> bool:
        """
        重複関係を設定してBを削除

        hash_a: 残す側（古い方）
        hash_b: 削除する側（新しい方）
        relationship=4: A is better
        do_default_content_merge=true: タグ/URL/ノートをBからAにマージ
        delete_b=true: Bを削除
        """
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"

        payload = {
            "relationships": [
                {
                    "hash_a": hash_a,
                    "hash_b": hash_b,
                    "relationship": 4,
                    "do_default_content_merge": True,
                    "delete_b": True,
                }
            ]
        }
        async with self.session.post(
            f"{self.api_url}/manage_file_relationships/set_file_relationships",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status == 200:
                return True
            else:
                text = await resp.text()
                logger.error(f"set_file_relationships失敗: {resp.status} {text}")
                return False

    async def remove_potentials(self, hash_a: str, hash_b: str) -> bool:
        """潜在重複ペアから除外（誤検知時に使用）"""
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"

        payload = {"hashes": [hash_a, hash_b]}
        async with self.session.post(
            f"{self.api_url}/manage_file_relationships/remove_potentials",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status == 200:
                return True
            else:
                text = await resp.text()
                logger.error(f"remove_potentials失敗: {resp.status} {text}")
                return False

    # --- メイン処理 ---

    def _determine_older(
        self, meta_a: Dict[str, Any], meta_b: Dict[str, Any]
    ) -> Tuple[str, str]:
        """
        import_timeで新旧を判定し、(残す側hash, 削除する側hash)を返す

        import_timeが同じ場合はファイルサイズが大きい方を残す
        """
        hash_a = meta_a.get("hash", "")
        hash_b = meta_b.get("hash", "")

        time_a = meta_a.get("import_time", 0)
        time_b = meta_b.get("import_time", 0)

        if time_a != time_b:
            # 古い方（import_timeが小さい方）を残す
            if time_a <= time_b:
                return hash_a, hash_b
            else:
                return hash_b, hash_a

        # import_timeが同じ場合 → ファイルサイズが大きい方を残す
        size_a = meta_a.get("size", 0)
        size_b = meta_b.get("size", 0)
        if size_a >= size_b:
            return hash_a, hash_b
        else:
            return hash_b, hash_a

    async def wait_for_potentials_stable(self) -> int:
        """
        potential discoveryが安定する（新しいペアが増えなくなる）まで待機

        Returns:
            安定後の潜在重複ペア数
        """
        logger.info("potential discoveryの安定を待機中...")
        elapsed = 0
        prev_count = -1

        while elapsed < self.polling_max_wait:
            count = await self.get_potentials_count()
            logger.info(f"  潜在重複ペア数: {count} (経過: {elapsed}秒)")

            if count == prev_count:
                logger.info(f"潜在重複ペア数が安定しました: {count}")
                return count

            prev_count = count
            await asyncio.sleep(self.polling_interval)
            elapsed += self.polling_interval

        logger.warning(
            f"最大待機時間({self.polling_max_wait}秒)に達しました。"
            f"現在のペア数: {prev_count}"
        )
        return prev_count

    async def process_duplicates(self) -> Dict[str, int]:
        """
        潜在重複ペアを全て処理する

        Returns:
            {"processed": N, "skipped": N, "failed": N, "total_pairs": N}
        """
        if not self.is_active:
            logger.info("Hydrus dedup機能は無効です")
            return {"processed": 0, "skipped": 0, "failed": 0, "total_pairs": 0}

        # 1. 潜在重複が安定するまで待機
        total_count = await self.wait_for_potentials_stable()

        if total_count == 0:
            logger.info("処理すべき潜在重複ペアはありません")
            return {"processed": 0, "skipped": 0, "failed": 0, "total_pairs": 0}

        # 2. ペアを取得
        pairs = await self.get_potential_pairs()
        logger.info(f"取得した潜在重複ペア数: {len(pairs)}")

        stats = {"processed": 0, "skipped": 0, "failed": 0, "total_pairs": len(pairs)}

        # 3. 各ペアを処理
        for i, pair_hashes in enumerate(pairs, 1):
            if len(pair_hashes) < 2:
                logger.warning(f"ペア{i}: ハッシュが不足しています: {pair_hashes}")
                stats["skipped"] += 1
                continue

            hash_1, hash_2 = pair_hashes[0], pair_hashes[1]

            try:
                # メタデータを取得
                meta_1, meta_2 = await asyncio.gather(
                    self.get_file_metadata(hash_1),
                    self.get_file_metadata(hash_2),
                )

                if not meta_1 or not meta_2:
                    logger.warning(
                        f"ペア{i}/{len(pairs)}: メタデータ取得失敗 "
                        f"({hash_1[:16]}... / {hash_2[:16]}...)"
                    )
                    stats["skipped"] += 1
                    continue

                # どちらかがローカルに存在しない場合はスキップ
                if not meta_1.get("is_local", False) or not meta_2.get("is_local", False):
                    logger.info(
                        f"ペア{i}/{len(pairs)}: ローカルに存在しないファイルがあるためスキップ"
                    )
                    stats["skipped"] += 1
                    continue

                # 新旧判定
                keep_hash, delete_hash = self._determine_older(meta_1, meta_2)

                keep_time = (meta_1 if meta_1["hash"] == keep_hash else meta_2).get("import_time", 0)
                del_time = (meta_1 if meta_1["hash"] == delete_hash else meta_2).get("import_time", 0)

                logger.info(
                    f"ペア{i}/{len(pairs)}: "
                    f"残す={keep_hash[:16]}...(import_time={keep_time}) "
                    f"削除={delete_hash[:16]}...(import_time={del_time})"
                )

                if self.dry_run:
                    logger.info(f"  [DRY RUN] 削除をスキップ")
                    stats["processed"] += 1
                    continue

                # 重複関係を設定（マージ→削除）
                success = await self.set_file_relationship(keep_hash, delete_hash)
                if success:
                    stats["processed"] += 1
                    logger.info(f"  重複解消完了")
                else:
                    stats["failed"] += 1
                    logger.error(f"  重複解消失敗")

            except Exception as e:
                logger.error(f"ペア{i}/{len(pairs)}: 処理エラー: {e}")
                stats["failed"] += 1

        logger.info(
            f"Hydrus dedup処理完了: "
            f"合計={stats['total_pairs']}, "
            f"処理={stats['processed']}, "
            f"スキップ={stats['skipped']}, "
            f"失敗={stats['failed']}"
        )
        return stats
