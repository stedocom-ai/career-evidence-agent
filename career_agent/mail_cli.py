"""Explicit email application commands, sharing the agent's local database."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .mail_store import MailStore
from .mailing import (compose, email_address, export_preview, preview_text, profile_path,
                      read_profile, read_resume, text_field, validate_snapshot, write_profile)
from .outlook import GraphClient, MailSendError, OutlookAuth


def register_mail_commands(sub):
    profile = sub.add_parser("mail-profile", help="设置发件邮箱、署名和简历路径")
    profile.add_argument("--email", required=True)
    profile.add_argument("--name", required=True)
    profile.add_argument("--resume", required=True)
    profile.add_argument("--transport", choices=["gmail-smtp", "outlook-desktop", "graph"], default="gmail-smtp")
    login = sub.add_parser("mail-login", help="可选 Graph 方式的微软网页登录授权")
    login.add_argument("--client-id")
    login.add_argument("--tenant", default=None)
    sub.add_parser("mail-check", help="检查指定发件邮箱是否已连接")
    draft = sub.add_parser("mail-draft", help="为一个指定岗位创建邮件和简历快照")
    draft.add_argument("--to", required=True)
    draft.add_argument("--company", required=True)
    draft.add_argument("--position", required=True)
    draft.add_argument("--job")
    draft.add_argument("--source-url")
    draft.add_argument("--subject")
    draft.add_argument("--body-file")
    for command, help_text in (("mail-preview", "查看已保存的邮件"), ("mail-send", "发送指定邮件一次")):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--id", required=True)
    edit = sub.add_parser("mail-edit", help="修改待发草稿的主题、正文或简历快照")
    edit.add_argument("--id", required=True)
    edit.add_argument("--subject")
    edit.add_argument("--body-file")
    edit.add_argument("--refresh-resume", action="store_true")
    sub.add_parser("mail-list", help="列出邮件申请及发送状态")
    resolve = sub.add_parser("mail-resolve", help="核对已发送记录后，处理结果不确定的申请")
    resolve.add_argument("--id", required=True)
    resolve.add_argument("--result", choices=["accepted", "not-sent"], required=True)
    resolve.add_argument("--note", required=True)
    demo = sub.add_parser("mail-demo", help="生成虚构邮件预览，不连接邮箱")
    demo.add_argument("--output", default=".local/mail-demo")


def _get_record(store, draft_id):
    record = store.get(draft_id)
    if record is None:
        raise ValueError("找不到这份邮件，请运行 mail-list 查看编号。")
    return record


def _auth(profile, db_path, *, client_id=None, tenant=None):
    return OutlookAuth(
        client_id=client_id or os.environ.get("OUTLOOK_CLIENT_ID") or profile.get("client_id", ""),
        tenant=tenant or os.environ.get("OUTLOOK_TENANT") or profile.get("tenant", "consumers"),
        cache_path=Path(db_path).parent / "outlook-cache.bin", username=profile["email"],
    )


def _client(profile, db_path):
    if profile["transport"] == "gmail-smtp":
        from .gmail import GmailSmtpClient
        return GmailSmtpClient(profile["email"], os.environ.get("GMAIL_APP_PASSWORD", ""))
    if profile["transport"] == "outlook-desktop":
        from .outlook_desktop import DesktopOutlookClient
        return DesktopOutlookClient(profile["email"])
    token = _auth(profile, db_path).acquire(interactive=False)
    return GraphClient(token)


def _verify_sender(client, expected):
    actual = client.get_sender()
    addresses = {str(actual.get(key) or "").casefold() for key in ("mail", "userPrincipalName")}
    if expected.casefold() not in addresses:
        raise ValueError("当前登录账户与草稿的发件邮箱不一致，请核对邮箱账户和 mail-profile。")


def send_draft(store, record, profile, db_path):
    if record["status"] == "accepted":
        print("这封邮件已交给发送服务，未重复发送。请查看邮箱的已发送或发件箱记录。")
        return 0
    if record["status"] in {"unknown", "sending"}:
        raise ValueError("上次发送结果待核实，请先检查已发送和发件箱，再使用 mail-resolve 记录结果。")
    validate_snapshot(record)
    if profile["email"].casefold() != record["payload"]["sender_email"].casefold():
        raise ValueError("草稿发件邮箱与当前 mail-profile 不一致。")
    client = _client(profile, db_path)
    _verify_sender(client, record["payload"]["sender_email"])
    record = store.claim(record["draft_id"])
    try:
        payload = {**record["payload"], "message_id": record["message_id"], "created_at": record["created_at"]}
        receipt = client.send_message(payload, record["attachment"], record["draft_id"])
    except MailSendError as exc:
        store.finish(record["draft_id"], record["attempt_id"], exc.outcome, error=str(exc))
        print(f"发送状态：{exc.outcome}\n{exc}")
        return 1
    except BaseException:
        # A process interruption after dispatch may leave a queued or sent message.
        store.finish(record["draft_id"], record["attempt_id"], "unknown", error="发送过程中断，需核对邮箱记录。")
        raise
    store.finish(record["draft_id"], record["attempt_id"], "accepted",
                 provider_reference=receipt.get("provider_reference"))
    print("邮件已交给发送服务（accepted）。请在邮箱的已发送和发件箱中核对处理结果。")
    return 0


def handle_mail_command(args):
    if args.command == "mail-profile":
        path, _ = read_resume(args.resume)
        profile = {"email": email_address(args.email), "name": text_field(args.name, "署名"),
                   "resume_path": str(path), "transport": args.transport}
        write_profile(args.db, profile)
        print(f"邮件配置已保存：{profile_path(args.db).resolve()}\n发信方式：{args.transport}")
        return 0
    if args.command == "mail-demo":
        output = Path(args.output)
        if output.exists() and any(output.iterdir()):
            raise ValueError("演示输出目录已有文件，请换一个空目录。")
        example = Path(__file__).resolve().parent.parent / "examples/demo-resume.pdf"
        profile = {"email": "applicant@example.com", "name": "示例候选人", "resume_path": str(example)}
        payload, attachment, key = compose(profile, "hr@example.com", "示例公司", "AI Agent 实习生")
        with MailStore(output / "demo.db") as store:
            record = store.create(payload, attachment, key)
            export_preview(record, output / record["draft_id"])
        (output / "run.json").write_text(json.dumps({"mode": "offline_preview", "status": "draft",
            "draft_id": record["draft_id"], "email_sent": False}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"虚构邮件预览已生成，状态为草稿：{output.resolve()}")
        return 0
    if args.command == "mail-list":
        with MailStore(args.db) as store:
            rows = [{"draft_id": r["draft_id"], "status": r["status"],
                     "recipient": r["payload"]["recipient"], "company": r["payload"]["company"],
                     "position": r["payload"]["position"], "error": r["error"]} for r in store.list()]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if args.command == "mail-resolve":
        with MailStore(args.db) as store:
            record = store.resolve(args.id, args.result == "accepted", args.note)
        print(f"核对结果已保存，状态为 {record['status']}。")
        return 0
    if args.command == "mail-preview":
        with MailStore(args.db) as store:
            record = _get_record(store, args.id)
            path = export_preview(record, Path(args.db).parent / "mail" / args.id)
            print(preview_text(record))
            print(f"邮件文件：{path.resolve() / 'message.eml'}")
        return 0
    if args.command == "mail-edit":
        if args.subject is None and args.body_file is None and not args.refresh_resume:
            raise ValueError("请指定 --subject、--body-file 或 --refresh-resume。")
        with MailStore(args.db) as store:
            record = _get_record(store, args.id)
            payload = dict(record["payload"])
            attachment = record["attachment"]
            if args.subject is not None:
                payload["subject"] = text_field(args.subject, "邮件主题", 300)
            if args.body_file:
                payload["body"] = text_field(Path(args.body_file).read_text(encoding="utf-8-sig"),
                                             "邮件正文", 20_000, multiline=True)
            if args.refresh_resume:
                profile = read_profile(args.db)
                if profile["email"].casefold() != payload["sender_email"].casefold():
                    raise ValueError("草稿与当前配置的发件邮箱不一致，未替换附件。")
                path, attachment = read_resume(profile["resume_path"])
                payload["resume_name"] = path.name
            record = store.update_draft(args.id, payload, attachment)
            export_preview(record, Path(args.db).parent / "mail" / args.id)
            print(preview_text(record))
        return 0
    profile = read_profile(args.db)
    if args.command == "mail-login":
        auth = _auth(profile, args.db, client_id=args.client_id, tenant=args.tenant)
        token = auth.acquire(interactive=True)
        _verify_sender(GraphClient(token), profile["email"])
        profile.update({"transport": "graph", "client_id": auth.client_id, "tenant": auth.tenant})
        write_profile(args.db, profile)
        print("微软 Graph 账户已授权，并已核对发件邮箱。")
        return 0
    if args.command == "mail-check":
        _verify_sender(_client(profile, args.db), profile["email"])
        print(f"发件账户检查通过：{profile['email']}")
        return 0
    if args.command == "mail-draft":
        if args.job:
            from .storage import Store
            with Store(args.db) as store:
                job = store.get_document(args.job)
                if not job or job["kind"] != "job":
                    raise ValueError("--job 需要一份已导入的岗位描述 ID。")
        body = Path(args.body_file).read_text(encoding="utf-8-sig") if args.body_file else None
        payload, attachment, key = compose(profile, args.to, args.company, args.position,
            job_id=args.job, source_url=args.source_url, subject=args.subject, body=body)
        with MailStore(args.db) as store:
            record = store.create(payload, attachment, key)
            output = export_preview(record, Path(args.db).parent / "mail" / record["draft_id"])
        print("已创建草稿。" if record["created"] else "这个邮箱和岗位已有记录，返回原邮件。")
        print(preview_text(record))
        print(f"预览文件：{output.resolve() / 'preview.txt'}")
        return 0
    if args.command == "mail-send":
        with MailStore(args.db) as store:
            return send_draft(store, _get_record(store, args.id), profile, args.db)
    raise ValueError("未知邮件命令。")
