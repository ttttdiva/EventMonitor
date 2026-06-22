#!/usr/bin/env python3
"""
Hydrus Client API: 選択中画像のcreator:タグで検索結果を新規ページに表示

サムネイル一覧で画像を選択した状態で実行すると、
その画像のcreator:タグを取得し、そのタグで検索した結果を
新しいfilesページに表示する。

ページ作成:
  空のfilesページがなければ、ctypesでHydrusにCtrl+Tを送信し
  新しいファイル検索ページを自動作成する。

使用方法:
    python scripts/hydrus/open_creator_page.py
    pythonw scripts/hydrus/open_creator_page.py   # コンソール非表示

必要なAPI権限:
    - Search for and Fetch Files (3)
    - Manage Pages (4)
"""

import ctypes
import ctypes.wintypes
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

# プロジェクトルート
PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)


# --- Windows API 定数 ---
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_T = 0x54


def show_error(text, title="Hydrus Creator Search"):
    """ポップアップでエラー表示"""
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)


def show_msg(text, title="Hydrus Creator Search"):
    """ポップアップでメッセージ表示"""
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)


def load_config():
    """config.yamlと.envからHydrus API設定を読み込む"""
    config_path = PROJECT_ROOT / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    hydrus = config.get("hydrus", {})
    api_url = hydrus.get("api_url", "http://127.0.0.1:45869")
    access_key = os.environ.get("HYDRUS_ACCESS_KEY") or hydrus.get("access_key", "")

    if not access_key:
        show_error("HYDRUS_ACCESS_KEYが未設定です")
        sys.exit(1)

    return api_url, access_key


def api_get(api_url, access_key, endpoint, params=None):
    """Hydrus Client APIにGETリクエストを送信"""
    headers = {"Hydrus-Client-API-Access-Key": access_key}
    url = f"{api_url}{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def api_post(api_url, access_key, endpoint, data):
    """Hydrus Client APIにPOSTリクエストを送信"""
    headers = {
        "Hydrus-Client-API-Access-Key": access_key,
        "Content-Type": "application/json",
    }
    url = f"{api_url}{endpoint}"
    resp = requests.post(url, headers=headers, json=data, timeout=10)
    resp.raise_for_status()
    return resp.json() if resp.text else {}


# --- ページツリー探索 ---

def find_selected_page_key(pages_data):
    """selectedなリーフページのpage_keyを再帰的に探す"""
    return _find_selected(pages_data.get("pages", {}))


def _find_selected(node):
    for sub in node.get("pages", []):
        if sub.get("selected", False):
            nested = sub.get("pages", [])
            if nested:
                result = _find_selected(sub)
                if result:
                    return result
            return sub.get("page_key")
    return None


def find_empty_files_page(api_url, access_key, pages_data, exclude_key=None):
    """空のfilesページ(page_type=6, num_files=0)のpage_keyを探す"""
    candidates = []
    _collect_file_search_pages(pages_data.get("pages", {}), candidates)

    for pk in candidates:
        if pk == exclude_key:
            continue
        try:
            data = api_get(
                api_url, access_key,
                "/manage_pages/get_page_info",
                params={"page_key": pk, "simple": "true"},
            )
            pi = data.get("page_info", {})
            if pi.get("page_type") != 6:
                continue
            media = pi.get("media", {})
            if media.get("num_files", -1) == 0:
                return pk
        except Exception:
            continue
    return None


def _collect_file_search_pages(node, results):
    """page_type=6のpage_keyを収集"""
    for sub in node.get("pages", []):
        if sub.get("page_type") == 6:
            results.append(sub.get("page_key"))
        nested = sub.get("pages", [])
        if nested:
            _collect_file_search_pages(sub, results)


# --- Hydrusウィンドウ操作 ---

def find_hydrus_window():
    """Hydrus Clientのウィンドウハンドルを取得"""
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    found = []

    def callback(hwnd, _lparam):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                if "hydrus client" in buf.value.lower():
                    found.append(hwnd)
        return True

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found[0] if found else None


def send_ctrl_t_to_hydrus():
    """
    HydrusウィンドウをフォアグラウンドにしてCtrl+Tを送信。
    keybd_eventを使用（Qt互換）。
    """
    hwnd = find_hydrus_window()
    if not hwnd:
        print("[DEBUG] Hydrusウィンドウが見つかりません")
        return False

    # Hydrusをフォアグラウンドに
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)

    # Ctrl+T を keybd_event で送信
    ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_T, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.keybd_event(VK_T, 0, KEYEVENTF_KEYUP, 0)
    ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    print("[DEBUG] Ctrl+T送信完了")
    return True


def create_new_page(api_url, access_key, original_key):
    """
    Ctrl+Tで新しいファイル検索ページを作成し、そのpage_keyを返す。
    最大3回リトライ。
    """
    if not send_ctrl_t_to_hydrus():
        return None

    # ページ作成を待つ（最大3秒）
    for i in range(6):
        time.sleep(0.5)
        pages_data = api_get(api_url, access_key, "/manage_pages/get_pages")
        pk = find_empty_files_page(
            api_url, access_key, pages_data, exclude_key=original_key
        )
        if pk:
            print(f"[DEBUG] 新規ページ検出: {pk}")
            return pk
        print(f"[DEBUG] 空ページ待ち... ({i+1}/6)")

    return None


# --- データ取得 ---

def get_selected_hashes(api_url, access_key, page_key):
    """選択中のファイルハッシュを取得"""
    data = api_get(
        api_url, access_key,
        "/manage_pages/get_page_info",
        params={"page_key": page_key, "simple": "false"},
    )
    media = data.get("page_info", {}).get("media", {})
    return media.get("hashes_selected", [])


def get_creator_tag(api_url, access_key, file_hash):
    """creator:タグを取得 (display_tags優先)"""
    data = api_get(
        api_url, access_key,
        "/get_files/file_metadata",
        params={"hashes": json.dumps([file_hash])},
    )
    for meta in data.get("metadata", []):
        for _svc, info in meta.get("tags", {}).items():
            for tags in info.get("display_tags", {}).values():
                for tag in tags:
                    if tag.startswith("creator:"):
                        return tag
            for tags in info.get("storage_tags", {}).values():
                for tag in tags:
                    if tag.startswith("creator:"):
                        return tag
    return None


def search_files(api_url, access_key, creator_tag):
    """ファイル検索"""
    data = api_get(
        api_url, access_key,
        "/get_files/search_files",
        params={"tags": json.dumps([creator_tag])},
    )
    return data.get("file_ids", [])


# --- メイン ---

def main():
    try:
        api_url, access_key = load_config()

        # 1. フォーカス中のページを取得
        pages_data = api_get(api_url, access_key, "/manage_pages/get_pages")
        original_key = find_selected_page_key(pages_data)
        if not original_key:
            show_error("フォーカスされたページがありません")
            sys.exit(1)

        # 2. 選択中ファイルのハッシュを取得
        hashes = get_selected_hashes(api_url, access_key, original_key)
        if not hashes:
            show_error("ファイルが選択されていません\n"
                       "サムネイルをクリックしてから実行してください")
            sys.exit(1)

        # 3. creator:タグ取得
        creator_tag = get_creator_tag(api_url, access_key, hashes[0])
        if not creator_tag:
            show_error("このファイルにcreator:タグがありません")
            sys.exit(1)
        print(f"[DEBUG] タグ: {creator_tag}")

        # 4. 検索
        file_ids = search_files(api_url, access_key, creator_tag)
        if not file_ids:
            show_msg(f"'{creator_tag}' に該当するファイルはありません")
            sys.exit(0)
        print(f"[DEBUG] 検索結果: {len(file_ids)}件")

        # 5. 空のfilesページを探す or 新規作成
        target_key = find_empty_files_page(
            api_url, access_key, pages_data, exclude_key=original_key
        )

        if not target_key:
            print("[DEBUG] 空ページなし → Ctrl+Tで新規作成")
            target_key = create_new_page(api_url, access_key, original_key)

        if not target_key:
            show_error(
                f"ページの作成に失敗しました。\n\n"
                f"手動で Ctrl+T を押して空ページを作成し、\n"
                f"もう一度実行してください。\n\n"
                f"検索タグ: {creator_tag}\n"
                f"該当ファイル: {len(file_ids)}件"
            )
            sys.exit(1)

        # 6. ファイルを追加 & フォーカス
        api_post(api_url, access_key, "/manage_pages/add_files", {
            "page_key": target_key,
            "file_ids": file_ids,
        })
        api_post(api_url, access_key, "/manage_pages/focus_page", {
            "page_key": target_key,
        })

        print(f"完了: '{creator_tag}' {len(file_ids)}件")

    except requests.ConnectionError:
        show_error("Hydrus APIに接続できません\n"
                   "Hydrusが起動しているか確認してください")
    except requests.HTTPError as e:
        show_error(f"API エラー: {e}")
    except Exception as e:
        show_error(f"予期しないエラー: {e}")


if __name__ == "__main__":
    main()
