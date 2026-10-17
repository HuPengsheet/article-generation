"""Local WeChat draft CLI: check, dry-run, upload, update. Never publish."""

import argparse
import fcntl
import sys
from pathlib import Path

from dotenv import load_dotenv

from shared.wechat_draft import (
    Account,
    WechatClient,
    WechatError,
    dry_run,
    load_article,
    sync_draft,
    verify_draft,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="本机凭据配置，不上传此文件")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("check", help="检查凭据、IP 白名单及草稿读取权限")
    for name in ("dry-run", "upload", "update", "verify"):
        command = sub.add_parser(name)
        command.add_argument("directory", type=Path)
        command.add_argument("--cover", required=True, help="相对文章目录的本地封面路径")
        command.add_argument("--title", help="短标题；本地保守限制为32字符，不自动截断")
        command.add_argument("--author", default=None)
        command.add_argument("--digest", default="")
        command.add_argument("--source-url", default="")
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    try:
        if args.action == "check":
            count = WechatClient(Account.from_environment()).check()
            print(f"连接与草稿读取权限检查通过，当前草稿数：{count}。")
            return 0
        import os

        author = (
            args.author if args.author is not None else os.environ.get("WECHAT_ACCOUNT_NAME", "")
        )
        article = load_article(
            args.directory,
            args.cover,
            title=args.title,
            author=author,
            digest=args.digest,
            source_url=args.source_url,
        )
        if args.action == "dry-run":
            dry_run(article)
            print(
                f"本地校验通过：{len(article.images)}张正文图片。预览："
                f"{article.directory / 'wechat-draft-preview.html'}；尚未上传。"
            )
            return 0
        with (article.directory / ".wechat-upload.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WechatError("此目录已有上传任务运行，请稍后重试。") from None
            client = WechatClient(Account.from_environment())
            result = (
                verify_draft(article, client)
                if args.action == "verify"
                else sync_draft(article, client, update=args.action == "update")
            )
        print(
            f"草稿保存并回读核对成功：{result['media_id']}；"
            f"{result['body_image_count']}张正文图片。请在公众号后台草稿箱查看。"
        )
        return 0
    except (WechatError, OSError, ValueError):
        # Only display errors we constructed; arbitrary transport/filesystem errors
        # may contain a credential-bearing URL or private data.
        error = sys.exc_info()[1]
        print(
            str(error)
            if isinstance(error, WechatError)
            else "本地文件或上传记录读取失败，请检查配置与文章目录。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
