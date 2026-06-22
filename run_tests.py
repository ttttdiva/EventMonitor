#!/usr/bin/env python3
"""
テスト実行用スクリプト
開発者が手軽にテストを実行できるようにするためのユーティリティ
"""
import subprocess
import sys
import os
import importlib.util
from pathlib import Path
import argparse


def run_command(cmd, description=""):
    """コマンドを実行し、結果を表示"""
    if description:
        print(f"\n{'='*50}")
        print(f"実行中: {description}")
        print(f"{'='*50}")
    
    print(f"コマンド: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False)
    
    if result.returncode != 0:
        print(f"❌ エラー: {description or 'コマンド実行'} が失敗しました")
        return False
    else:
        print(f"✅ 成功: {description or 'コマンド実行'} が完了しました")
        return True


def has_module(module_name):
    """指定したモジュールが利用可能か確認"""
    return importlib.util.find_spec(module_name) is not None


def has_test_files(path):
    """指定ディレクトリに pytest 対象ファイルがあるか確認"""
    test_path = Path(path)
    return test_path.exists() and any(test_path.rglob("test_*.py"))


def has_marked_tests(marker_name, root="tests"):
    """指定マーカーを持つテストが存在するか確認"""
    root_path = Path(root)
    if not root_path.exists():
        return False

    patterns = (
        f"@pytest.mark.{marker_name}",
        f"pytestmark = pytest.mark.{marker_name}",
        f'pytestmark=pytest.mark.{marker_name}',
    )

    for test_file in root_path.rglob("test_*.py"):
        content = test_file.read_text(encoding="utf-8")
        if any(pattern in content for pattern in patterns):
            return True

    return False


def python_module_command(module_name, *args):
    """現在の Python 環境でモジュールを実行するコマンドを返す"""
    return [sys.executable, '-m', module_name, *args]


def check_environment():
    """テスト実行環境をチェック"""
    print("環境チェック中...")
    
    # .env.test ファイルの確認
    env_test_file = Path('.env.test')
    if not env_test_file.exists():
        print("❌ .env.test ファイルが見つかりません")
        print("   テスト用環境変数ファイルを作成してください")
        return False
    
    # pytest の確認
    try:
        import pytest
        print(f"✅ pytest {pytest.__version__} が利用可能です")
    except ImportError:
        print("❌ pytest がインストールされていません")
        print("   pip install -r requirements.txt を実行してください")
        return False
    
    return True


def main():
    parser = argparse.ArgumentParser(description="EventMonitor テスト実行ツール")
    parser.add_argument('--unit', action='store_true', help='単体テストのみ実行')
    parser.add_argument('--integration', action='store_true', help='結合テストのみ実行') 
    parser.add_argument('--slow', action='store_true', help='スローテストも実行')
    parser.add_argument('--cov', action='store_true', help='カバレッジレポート生成')
    parser.add_argument('--html', action='store_true', help='HTMLカバレッジレポート生成')
    parser.add_argument('--quick', action='store_true', help='クイックテスト（単体テストのみ、並列実行）')
    parser.add_argument('--lint', action='store_true', help='コード品質チェック')
    parser.add_argument('--security', action='store_true', help='セキュリティスキャン')
    parser.add_argument('--all', action='store_true', help='全てのチェックを実行')
    
    args = parser.parse_args()
    
    # 引数が何も指定されていない場合、基本テストを実行
    if not any([args.unit, args.integration, args.slow, args.quick, args.lint, args.security, args.all]):
        args.unit = True
        args.integration = True
        args.cov = True
    
    # プロジェクトルートに移動
    os.chdir(Path(__file__).parent)
    
    # 環境チェック
    if not check_environment():
        sys.exit(1)
    
    success_count = 0
    total_count = 0
    
    # クイックテスト
    if args.quick:
        total_count += 1
        cmd = python_module_command('pytest', 'tests/unit/', '-v')
        if has_module('xdist'):
            cmd.extend(['-n', 'auto'])
        else:
            print("ℹ️ pytest-xdist が未導入のため、クイックテストは直列で実行します")
        if run_command(cmd, "クイックテスト（単体テスト並列実行）"):
            success_count += 1
    
    # 単体テスト
    elif args.unit or args.all:
        total_count += 1
        cmd = python_module_command('pytest', 'tests/unit/', '-v')
        if args.cov or args.html or args.all:
            cmd.extend(['--cov=src', '--cov-report=term-missing'])
            if args.html or args.all:
                cmd.append('--cov-report=html')
        
        if run_command(cmd, "単体テスト"):
            success_count += 1
    
    # 結合テスト
    if args.integration or args.all:
        if has_test_files('tests/integration'):
            total_count += 1
            cmd = python_module_command('pytest', 'tests/integration/', '-v')
            if args.cov or args.html or args.all:
                cmd.extend(['--cov=src', '--cov-append', '--cov-report=term-missing'])
                if args.html or args.all:
                    cmd.append('--cov-report=html')
            
            if run_command(cmd, "結合テスト"):
                success_count += 1
        else:
            print("ℹ️ tests/integration/ が存在しないため、結合テストはスキップします")

    # スローテスト
    if args.slow or args.all:
        if has_marked_tests('slow'):
            total_count += 1
            cmd = python_module_command('pytest', 'tests/', '-v', '-m', 'slow', '--timeout=300')
            if run_command(cmd, "スローテスト"):
                success_count += 1
        else:
            print("ℹ️ slow マーカー付きテストがないため、スローテストはスキップします")
    
    # コード品質チェック
    if args.lint or args.all:
        checks = [
            (['flake8', 'src/', 'tests/', '--max-line-length=100', '--ignore=E203,W503'], "flake8 - コードスタイルチェック"),
            (['black', '--check', '--diff', 'src/', 'tests/'], "black - フォーマットチェック"),
            (['isort', '--check-only', '--diff', 'src/', 'tests/'], "isort - インポート順序チェック")
        ]
        
        for cmd, desc in checks:
            total_count += 1
            try:
                if run_command(cmd, desc):
                    success_count += 1
            except FileNotFoundError:
                print(f"⚠️  {cmd[0]} がインストールされていません。スキップします。")
                print(f"   インストール: pip install {cmd[0]}")
    
    # セキュリティスキャン
    if args.security or args.all:
        security_checks = [
            (['safety', 'check', '--json'], "safety - 依存関係脆弱性チェック"),
            (['bandit', '-r', 'src/', '-f', 'json'], "bandit - セキュリティスキャン")
        ]
        
        for cmd, desc in security_checks:
            total_count += 1
            try:
                # セキュリティツールは警告があっても継続
                result = subprocess.run(cmd, capture_output=True, text=True)
                print(f"\n{'='*50}")
                print(f"実行中: {desc}")
                print(f"{'='*50}")
                print(f"コマンド: {' '.join(cmd)}")
                
                if result.stdout:
                    print("出力:")
                    print(result.stdout)
                if result.stderr:
                    print("エラー/警告:")
                    print(result.stderr)
                
                print(f"✅ 完了: {desc}")
                success_count += 1
            except FileNotFoundError:
                print(f"⚠️  {cmd[0]} がインストールされていません。スキップします。")
                print(f"   インストール: pip install {cmd[0]}")
    
    # 結果サマリー
    print(f"\n{'='*50}")
    print("テスト実行結果サマリー")
    print(f"{'='*50}")
    print(f"成功: {success_count}/{total_count} チェック")
    
    if success_count == total_count:
        print("🎉 全てのチェックが成功しました！")
        
        # カバレッジレポートの場所を表示
        if args.html or args.all:
            html_report = Path('htmlcov/index.html')
            if html_report.exists():
                print(f"\n📊 HTMLカバレッジレポート: {html_report.absolute()}")
        
        sys.exit(0)
    else:
        print("❌ 一部のチェックが失敗しました")
        sys.exit(1)


if __name__ == "__main__":
    main()
