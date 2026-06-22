#!/usr/bin/env python3
"""
gallery-dl wrapper for pysqlite3 environment
Usage: python src/gallery_dl_wrapper.py [gallery-dl options] URL
"""

import sys
import os
import re

try:
    # pysqlite3を標準のsqlite3より先にインポート（可能な場合）
    __import__('pysqlite3')
    sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
    # Windows等でpysqlite3がない場合は標準のsqlite3を使用
    pass

# 標準エラー出力を抑制（プログレス表示など）
os.environ['PYTHONWARNINGS'] = 'ignore'

# --- gallery-dl XClientTxId モンキーパッチ ---
# Twitter側のJS構造変更により "ondemand.s":"HASH" 形式が廃止され、
# チャンクIDマップ経由でハッシュを取得する必要がある。
# gallery-dl上流で修正されるまでの暫定対応。
try:
    from gallery_dl import transaction_id, text as gdl_text

    _original_initialize = transaction_id.ClientTransaction.initialize

    def _patched_initialize(self, extractor, homepage=None):
        """ondemand.sハッシュ抽出をチャンクIDマップ対応に拡張"""
        if homepage is None:
            homepage = extractor.request("https://x.com/").text

        # 旧形式: "ondemand.s":"HASH" が存在すればそのまま使う
        ondemand_s = gdl_text.extr(homepage, '"ondemand.s":"', '"')

        if not ondemand_s:
            # 新形式: チャンクIDマップから探索
            # HTMLに 20113:"ondemand.s" のようにチャンクIDが振られている
            pos = homepage.find('"ondemand.s"')
            if pos >= 0:
                before = homepage[max(0, pos - 20):pos]
                m = re.search(r'(\d+):\s*$', before)
                if m:
                    chunk_id = m.group(1)
                    # チャンクIDに対応するハッシュを別のマップから取得
                    hash_match = re.search(
                        rf'{chunk_id}:"([a-f0-9]+)"', homepage
                    )
                    if hash_match:
                        ondemand_s = hash_match.group(1)

        if ondemand_s:
            # ondemand_sが見つかった場合は通常フローへ
            # ただしinitialize内部でtext.extrを使うので、一時的に差し替え
            original_extr = gdl_text.extr

            def _patched_extr(txt, begin, end, pos=0):
                if begin == '"ondemand.s":"':
                    return ondemand_s
                return original_extr(txt, begin, end, pos)

            gdl_text.extr = _patched_extr
            try:
                _original_initialize(self, extractor, homepage)
            finally:
                gdl_text.extr = original_extr
        else:
            # フォールバック: オリジナルのまま（エラーになる可能性あり）
            _original_initialize(self, extractor, homepage)

    transaction_id.ClientTransaction.initialize = _patched_initialize
except (ImportError, AttributeError):
    pass

import gallery_dl

if __name__ == "__main__":
    # gallery-dlのメイン関数を実行
    try:
        sys.exit(gallery_dl.main())
    except SystemExit as e:
        # 正常終了の場合はエラーコード0を返す
        if e.code in (0, None):
            sys.exit(0)
        else:
            sys.exit(e.code)