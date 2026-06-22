#!/usr/bin/env python3
"""
Hydrus視覚的重複検知（perceptual hash）バッチ後処理スクリプト

Hydrusのperceptual hashによる潜在重複ペアを処理し、
古い方を残して新しい方を削除する（タグ/URL/ノートは自動マージ）。

使用方法:
    python scripts/hydrus/dedup.py [--dry-run] [--hamming N]

オプション:
    --dry-run        削除せずにログのみ出力
    --hamming N      ハミング距離（デフォルト: config.yamlの値）
    --no-wait        potential discovery安定待ちをスキップ
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

# プロジェクトルートをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

env_path = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_dedup import HydrusDedup


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main():
    parser = argparse.ArgumentParser(
        description="Hydrus perceptual hash重複検知バッチ処理"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="削除せずにログのみ出力"
    )
    parser.add_argument(
        "--hamming", type=int, help="ハミング距離（0-8, デフォルト: config.yamlの値）"
    )
    parser.add_argument(
        "--no-wait", action="store_true", help="potential discovery安定待ちをスキップ"
    )
    parser.add_argument(
        "--log-level", default="INFO", help="ログレベル (DEBUG/INFO/WARNING/ERROR)"
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger("HydrusDedupScript")

    # 設定読み込み
    config_path = PROJECT_ROOT / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # CLIオプションで設定を上書き
    if args.dry_run:
        config.setdefault("hydrus", {}).setdefault("dedup", {})["dry_run"] = True
    if args.hamming is not None:
        config.setdefault("hydrus", {}).setdefault("dedup", {})[
            "max_hamming_distance"
        ] = args.hamming
    if args.no_wait:
        config.setdefault("hydrus", {}).setdefault("dedup", {})["polling_max_wait"] = 0

    dedup = HydrusDedup(config)

    if not dedup.is_active:
        logger.error(
            "Hydrus dedup機能が無効です。config.yamlのhydrus.enabled=trueかつhydrus.dedup.enabled=trueを確認してください"
        )
        sys.exit(1)

    async with dedup:
        if not dedup._session_key:
            logger.error("Hydrus APIに接続できませんでした。Hydrus Clientが起動していることを確認してください")
            sys.exit(1)

        logger.info("Hydrus APIに接続しました")

        if args.dry_run:
            logger.info("[DRY RUN モード] 実際の削除は行いません")

        # 処理実行
        stats = await dedup.process_duplicates()

        # 結果表示
        print(f"\n{'='*50}")
        print(f"Hydrus dedup処理完了")
        print(f"  合計ペア数: {stats['total_pairs']}")
        print(f"  処理済み:   {stats['processed']}")
        print(f"  スキップ:   {stats['skipped']}")
        print(f"  失敗:       {stats['failed']}")
        print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
