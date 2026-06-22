"""
パス変換ユーティリティ
相対パスと絶対パスの変換を一元管理
"""

from pathlib import Path
from typing import Union, Optional, List
import logging

logger = logging.getLogger(__name__)


def to_absolute_path(relative_path: Union[str, Path], config: dict) -> Path:
    """
    相対パスを絶対パスに変換

    Args:
        relative_path: 相対パス (例: "images/username/file.jpg")
        config: 設定辞書（media_storage設定を含む）

    Returns:
        絶対パス (例: Path("/mnt/f/48_EventMonitor_log/images/username/file.jpg"))
    """
    path = Path(relative_path)

    # 既に絶対パスの場合はそのまま返す
    if path.is_absolute():
        return path

    str_path = str(path).replace('\\', '/')

    # images/で始まる場合
    if str_path.startswith('images/'):
        images_path = config.get('media_storage', {}).get('images_path')
        if not images_path:
            logger.error("config.yaml: media_storage.images_path is not configured")
            return path  # 変換できない場合は元のパスを返す
        images_base = Path(images_path)
        # images/を除いた部分を結合
        relative_part = str_path.replace('images/', '', 1)
        return images_base / relative_part

    # videos/で始まる場合
    elif str_path.startswith('videos/'):
        videos_path = config.get('media_storage', {}).get('videos_path')
        if not videos_path:
            logger.error("config.yaml: media_storage.videos_path is not configured")
            return path  # 変換できない場合は元のパスを返す
        videos_base = Path(videos_path)
        # videos/を除いた部分を結合
        relative_part = str_path.replace('videos/', '', 1)
        return videos_base / relative_part

    # その他の場合はそのまま返す
    return path


def to_relative_path(absolute_path: Union[str, Path], config: dict) -> str:
    """
    絶対パスを相対パスに変換

    Args:
        absolute_path: 絶対パス (例: "/mnt/f/48_EventMonitor_log/images/username/file.jpg")
        config: 設定辞書（media_storage設定を含む）

    Returns:
        相対パス文字列 (例: "images/username/file.jpg")
    """
    str_path = str(absolute_path)

    # 設定から絶対パスのベースを取得
    images_path = config.get('media_storage', {}).get('images_path')
    videos_path = config.get('media_storage', {}).get('videos_path')

    # images_pathを含む場合
    if images_path and images_path in str_path:
        # /mnt/f/48_EventMonitor_log/images/ -> images/
        return str_path.replace(images_path + '/', 'images/')

    # videos_pathを含む場合
    if videos_path and videos_path in str_path:
        # /mnt/f/48_EventMonitor_log/videos/ -> videos/
        return str_path.replace(videos_path + '/', 'videos/')

    # ハードコードされたパスのフォールバック（後方互換性のため）
    if '/mnt/f/48_EventMonitor_log/images/' in str_path:
        return str_path.replace('/mnt/f/48_EventMonitor_log/', '')
    if '/mnt/f/48_EventMonitor_log/videos/' in str_path:
        return str_path.replace('/mnt/f/48_EventMonitor_log/', '')

    # 変換できない場合はそのまま返す
    return str_path


def convert_paths_to_absolute(paths: List[Union[str, Path]], config: dict) -> List[Path]:
    """
    パスのリストを絶対パスに変換

    Args:
        paths: パスのリスト
        config: 設定辞書

    Returns:
        絶対パスのリスト
    """
    return [to_absolute_path(p, config) for p in paths]


def convert_paths_to_relative(paths: List[Union[str, Path]], config: dict) -> List[str]:
    """
    パスのリストを相対パスに変換

    Args:
        paths: パスのリスト
        config: 設定辞書

    Returns:
        相対パスのリスト
    """
    return [to_relative_path(p, config) for p in paths]


def get_media_base_paths(config: dict) -> tuple[Path, Path]:
    """
    設定からメディアのベースパスを取得

    Args:
        config: 設定辞書

    Returns:
        (images_base_path, videos_base_path)のタプル
    """
    images_path = config.get('media_storage', {}).get('images_path', 'images')
    videos_path = config.get('media_storage', {}).get('videos_path', 'videos')
    return Path(images_path), Path(videos_path)