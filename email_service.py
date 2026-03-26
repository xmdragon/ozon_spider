"""
OZON 登录邮件服务

使用 QQ/163 邮箱 IMAP 来接收验证码邮件
"""

import smtplib
import imaplib
import email
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from email.utils import parseaddr
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
import re
import logging
import asyncio

logger = logging.getLogger(__name__)

OZON_VERIFICATION_SUBJECT = "Подтверждение учетных данных Ozon"
OZON_VERIFICATION_SENDER = "mailer@sender.ozon.ru"


# 邮箱服务器配置
EMAIL_PROVIDERS = {
    'qq': {
        'smtp_host': 'smtp.qq.com',
        'smtp_port': 587,
        'imap_host': 'imap.qq.com',
        'imap_port': 993,
    },
    '163': {
        'smtp_host': 'smtp.163.com',
        'smtp_port': 465,
        'imap_host': 'imap.163.com',
        'imap_port': 993,
    },
}


def _detect_email_provider(email: str) -> str:
    """根据邮箱地址检测邮箱服务商"""
    email_lower = email.lower()
    if '@qq.com' in email_lower:
        return 'qq'
    elif '@163.com' in email_lower or '@126.com' in email_lower:
        return '163'
    else:
        raise ValueError(f"不支持的邮箱类型: {email}，仅支持 QQ、163/126")


class EmailService:
    """邮件服务类（支持 QQ、163 邮箱）"""

    def __init__(self, email: str, app_password: str):
        """
        初始化邮件服务

        Args:
            email: 邮箱地址（支持 QQ、163/126）
            app_password: 应用专用密码/授权码
        """
        self.email = email
        self.app_password = app_password.replace(" ", "")  # 去除空格
        self._smtp_conn: Optional[smtplib.SMTP] = None
        self._imap_conn: Optional[imaplib.IMAP4_SSL] = None

        # 自动检测邮箱服务商
        self._provider = _detect_email_provider(email)
        self._config = EMAIL_PROVIDERS[self._provider]
        logger.info(f"邮箱服务商: {self._provider}, IMAP: {self._config['imap_host']}")

    def connect_smtp(self) -> smtplib.SMTP:
        """连接 SMTP 服务器"""
        if self._smtp_conn is None:
            self._smtp_conn = smtplib.SMTP(self._config['smtp_host'], self._config['smtp_port'], timeout=15)
            self._smtp_conn.starttls()
            self._smtp_conn.login(self.email, self.app_password)
            logger.info(f"SMTP 连接成功: {self.email}")
        return self._smtp_conn

    def connect_imap(self) -> imaplib.IMAP4_SSL:
        """连接 IMAP 服务器"""
        if self._imap_conn is None:
            context = ssl.create_default_context()
            self._imap_conn = imaplib.IMAP4_SSL(
                self._config['imap_host'],
                self._config['imap_port'],
                ssl_context=context,
                timeout=30
            )
            self._imap_conn.login(self.email, self.app_password)
            self._send_imap_id_if_needed(self._imap_conn)
            logger.info(f"IMAP 连接成功: {self.email} ({self._provider})")
        return self._imap_conn

    def _send_imap_id_if_needed(self, imap_conn: imaplib.IMAP4_SSL) -> None:
        """163/126 邮箱要求客户端在登录后发送 IMAP ID 信息。"""
        if self._provider != '163':
            return

        capabilities = {
            item.decode() if isinstance(item, bytes) else str(item)
            for item in getattr(imap_conn, "capabilities", ())
        }
        if "ID" not in capabilities:
            raise imaplib.IMAP4.error("163 IMAP 服务器未声明 ID capability")

        imap_id = (
            f'("name" "ozon_spider" '
            f'"version" "1.0.0" '
            f'"vendor" "ozon_spider" '
            f'"support-email" "{self.email}")'
        )
        typ, data = imap_conn.xatom("ID", imap_id)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"163 IMAP ID 发送失败: {data}")

    def _parse_mailbox_list_item(self, raw_item: Any) -> Optional[Dict[str, Any]]:
        text = raw_item.decode(errors='ignore') if isinstance(raw_item, bytes) else str(raw_item)
        match = re.match(r'\((?P<flags>[^)]*)\)\s+"(?P<delimiter>[^"]*)"\s+(?P<name>.+)$', text)
        if not match:
            return None

        flags_str = match.group("flags").strip()
        flags = flags_str.split() if flags_str else []
        name = match.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]

        return {
            "flags": flags,
            "delimiter": match.group("delimiter"),
            "name": name,
            "raw": text,
        }

    def list_mailboxes(self) -> List[Dict[str, Any]]:
        imap = self.connect_imap()
        typ, boxes = imap.list()
        if typ != "OK":
            raise imaplib.IMAP4.error(f"列出邮箱文件夹失败: {boxes}")

        mailboxes = []
        for raw_item in boxes or []:
            parsed = self._parse_mailbox_list_item(raw_item)
            if parsed:
                mailboxes.append(parsed)
        return mailboxes

    def _resolve_special_folder(self, special_flag: str, fallback: Optional[str] = None) -> Optional[str]:
        for mailbox in self.list_mailboxes():
            if special_flag in mailbox["flags"]:
                return mailbox["name"]
        return fallback

    def get_check_folders(self, check_spam: bool = True) -> List[str]:
        folders = ["INBOX"]
        if check_spam:
            spam_folder = (
                self._resolve_special_folder(r'\Junk')
                or self._resolve_special_folder(r'\Spam')
                or "Junk"
            )
            if spam_folder not in folders:
                folders.append(spam_folder)
        return folders

    def select_folder(self, folder: str) -> None:
        imap = self.connect_imap()
        typ, data = imap.select(folder)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"选择文件夹失败: {folder}: {data}")

    def list_email_ids(self, folder: str = "INBOX") -> List[int]:
        imap = self.connect_imap()
        self.select_folder(folder)
        typ, message_numbers = imap.search(None, "ALL")
        if typ != "OK":
            raise imaplib.IMAP4.error(f"搜索邮件失败: {folder}: {message_numbers}")
        if not message_numbers or not message_numbers[0]:
            return []
        return [int(x) for x in message_numbers[0].split()]

    def _parse_email_message(self, email_id: Any, msg) -> Dict[str, Any]:
        from_header = msg.get("From", "")
        subject = ""
        subject_header = msg.get("Subject", "")
        if subject_header:
            decoded = decode_header(subject_header)
            subject = "".join(
                part.decode(encoding or 'utf-8') if isinstance(part, bytes) else part
                for part, encoding in decoded
            )

        return {
            "id": email_id.decode() if isinstance(email_id, bytes) else str(email_id),
            "from": from_header,
            "subject": subject,
            "date": msg.get("Date", ""),
            "body": self._get_email_body(msg),
        }

    def is_ozon_verification_email(self, from_header: str, subject: str) -> bool:
        sender_email = parseaddr(from_header)[1].strip().lower()
        normalized_subject = " ".join(subject.split())
        return (
            sender_email == OZON_VERIFICATION_SENDER
            and normalized_subject == OZON_VERIFICATION_SUBJECT
        )

    def parse_email_datetime(self, date_header: str) -> Optional[datetime]:
        try:
            dt = email.utils.parsedate_to_datetime(date_header)
        except Exception:
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def is_email_within_seconds(self, date_header: str, max_age_seconds: int) -> bool:
        dt = self.parse_email_datetime(date_header)
        if dt is None:
            return False
        age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
        return 0 <= age_seconds <= max_age_seconds

    def fetch_email_by_id(self, folder: str, email_id: Any) -> Optional[Dict[str, Any]]:
        imap = self.connect_imap()
        self.select_folder(folder)
        typ, msg_data = imap.fetch(str(email_id).encode(), "(RFC822)")
        if typ != "OK":
            raise imaplib.IMAP4.error(f"获取邮件失败: {folder} #{email_id}: {msg_data}")

        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                return self._parse_email_message(email_id, msg)
        return None

    def disconnect(self):
        """断开所有连接"""
        if self._smtp_conn:
            try:
                self._smtp_conn.quit()
            except Exception:
                pass
            self._smtp_conn = None

        if self._imap_conn:
            try:
                self._imap_conn.logout()
            except Exception:
                pass
            self._imap_conn = None

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html: bool = False
    ) -> bool:
        """
        发送邮件

        Args:
            to: 收件人地址
            subject: 邮件主题
            body: 邮件正文
            html: 是否为 HTML 格式

        Returns:
            是否发送成功
        """
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email
            msg['To'] = to
            msg['Subject'] = subject

            content_type = 'html' if html else 'plain'
            msg.attach(MIMEText(body, content_type, 'utf-8'))

            smtp = self.connect_smtp()
            smtp.sendmail(self.email, to, msg.as_string())

            logger.info(f"邮件发送成功: {to}, 主题: {subject}")
            return True

        except Exception as e:
            logger.error(f"邮件发送失败: {e}")
            return False

    def get_recent_emails(
        self,
        folder: str = "INBOX",
        sender_filter: Optional[str] = None,
        subject_filter: Optional[str] = None,
        minutes: int = 10,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        获取最近的邮件

        Args:
            folder: 邮件文件夹（INBOX, Spam 等）
            sender_filter: 发件人过滤（支持部分匹配）
            subject_filter: 主题过滤（支持部分匹配）
            minutes: 获取最近多少分钟内的邮件
            limit: 最多返回多少封邮件

        Returns:
            邮件列表 [{"from": "", "subject": "", "date": "", "body": ""}, ...]
        """
        emails = []

        try:
            imap = self.connect_imap()
            self.select_folder(folder)

            # 搜索最近的邮件
            since_date = (datetime.now() - timedelta(minutes=minutes)).strftime("%d-%b-%Y")
            search_criteria = f'(SINCE "{since_date}")'

            if sender_filter:
                search_criteria = f'(FROM "{sender_filter}" SINCE "{since_date}")'

            typ, message_numbers = imap.search(None, search_criteria)
            if typ != "OK":
                raise imaplib.IMAP4.error(f"搜索邮件失败: {folder}: {message_numbers}")

            if not message_numbers[0]:
                return emails

            # 获取邮件ID列表（最新的在前）
            email_ids = message_numbers[0].split()[-limit:]
            email_ids.reverse()

            for email_id in email_ids:
                typ, msg_data = imap.fetch(email_id, "(RFC822)")
                if typ != "OK":
                    logger.warning("获取邮件失败: %s #%s: %s", folder, email_id, msg_data)
                    continue

                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        parsed_mail = self._parse_email_message(email_id, msg)
                        subject = parsed_mail["subject"]

                        # 主题过滤
                        if subject_filter and subject_filter.lower() not in subject.lower():
                            continue

                        emails.append(parsed_mail)

            return emails

        except Exception as e:
            logger.error(f"获取邮件失败: {e}")
            return emails

    def find_latest_ozon_verification_email(
        self,
        max_age_seconds: int = 60,
        check_spam: bool = True,
        minutes: int = 10,
        limit: int = 20,
    ) -> Optional[Dict[str, Any]]:
        """返回最近时间窗口内最新的一封 Ozon 验证码邮件及其元数据。"""
        latest: Optional[Dict[str, Any]] = None
        latest_dt: Optional[datetime] = None

        for folder in self.get_check_folders(check_spam=check_spam):
            try:
                emails = self.get_recent_emails(
                    folder=folder,
                    minutes=minutes,
                    limit=limit,
                )
            except Exception as e:
                logger.warning("检查文件夹 %s 失败: %s", folder, e)
                continue

            for mail in emails:
                if not self.is_ozon_verification_email(mail["from"], mail["subject"]):
                    continue
                if not self.is_email_within_seconds(mail["date"], max_age_seconds):
                    continue
                code = self._extract_ozon_code(mail["body"], mail["subject"])
                if not code:
                    continue
                dt = self.parse_email_datetime(mail["date"])
                if dt is None:
                    continue
                enriched = dict(mail)
                enriched["folder"] = folder
                enriched["code"] = code
                enriched["parsed_date"] = dt
                if latest_dt is None or dt > latest_dt:
                    latest = enriched
                    latest_dt = dt

        return latest

    def _get_email_body(self, msg) -> str:
        """提取邮件正文"""
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                if content_type == "text/plain" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or 'utf-8'
                        body = payload.decode(charset, errors='ignore')
                        break
                elif content_type == "text/html" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or 'utf-8'
                        body = payload.decode(charset, errors='ignore')
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                body = payload.decode(charset, errors='ignore')

        return body

    def find_ozon_verification_code(
        self,
        minutes: int = 5,
        check_spam: bool = True
    ) -> Optional[str]:
        """
        查找 OZON 验证码邮件

        Args:
            minutes: 查找最近多少分钟内的邮件
            check_spam: 是否同时检查垃圾邮件文件夹

        Returns:
            验证码字符串，未找到返回 None
        """
        latest = self.find_latest_ozon_verification_email(
            max_age_seconds=60,
            check_spam=check_spam,
            minutes=minutes,
            limit=20,
        )
        if latest:
            logger.info("找到 OZON 验证码: %s (来自 %s)", latest["code"], latest["folder"])
            return str(latest["code"])
        return None

    def _extract_ozon_code(self, body: str, subject: str) -> Optional[str]:
        """
        从邮件内容中提取 OZON 验证码

        Args:
            body: 邮件正文
            subject: 邮件主题

        Returns:
            验证码字符串
        """
        text = f"{subject}\n{body}"

        # OZON HTML 邮件格式：验证码在按钮样式中
        # 格式: -->558511<!--  或者在 <span>558511</span> 中
        patterns = [
            # HTML 格式：-->123456<!-- (OZON 按钮样式)
            r'-->\s*(\d{6})\s*<!--',
            # HTML 格式：使用код后的6位数字
            r'используйте код[^0-9]*(\d{6})',
            # 纯文本格式
            r'(?:код|code|验证码|Код подтверждения)[:\s]+(\d{4,6})',
            # HTML span 标签中的 6 位数字（前后有标签边界）
            r'>\s*(\d{6})\s*<',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    def wait_for_ozon_code(
        self,
        timeout: int = 55,
        interval: int = 3,
        check_spam: bool = True
    ) -> Optional[str]:
        """
        等待并获取 OZON 验证码

        注意：OZON 验证码有效期只有 1 分钟，所以默认超时设为 55 秒

        Args:
            timeout: 超时时间（秒），默认 55 秒（留 5 秒填写时间）
            interval: 检查间隔（秒），默认 3 秒
            check_spam: 是否检查垃圾邮件

        Returns:
            验证码字符串，超时返回 None
        """
        import time

        start_time = time.time()
        logger.info(f"开始等待 OZON 验证码，超时: {timeout}秒（验证码有效期 1 分钟）")

        while time.time() - start_time < timeout:
            # 只查找最近 1 分钟内的邮件（验证码有效期）
            code = self.find_ozon_verification_code(
                minutes=1,
                check_spam=check_spam
            )

            if code:
                return code

            logger.debug(f"未找到验证码，{interval}秒后重试...")
            time.sleep(interval)

        logger.warning(f"等待验证码超时（{timeout}秒）")
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


async def get_email_service_from_config() -> Optional[EmailService]:
    """
    从系统配置中获取邮件服务实例

    Returns:
        EmailService 实例，配置不存在返回 None
    """
    import json
    from ef_core.database import get_db_manager
    from sqlalchemy import text

    try:
        db_manager = get_db_manager()
        async with db_manager.get_session() as db:
            # 使用直接 SQL 查询避免 ORM 模型依赖问题
            result = await db.execute(
                text("SELECT setting_value FROM ozon_global_settings WHERE setting_key = 'ozon_login'")
            )
            row = result.fetchone()

            if not row:
                logger.warning("未找到 ozon_login 配置")
                return None

            config = row[0]
            if isinstance(config, str):
                config = json.loads(config)

            email_address = config.get("email", "")
            app_password = config.get("app_password", "")

            if not email_address or not app_password:
                logger.warning("邮箱地址或应用密码未配置")
                return None

            return EmailService(email_address, app_password)

    except Exception as e:
        logger.error(f"获取邮件服务配置失败: {e}")
        return None


def get_email_service_sync(email_address: str, app_password: str) -> EmailService:
    """
    直接创建邮件服务实例（同步版本）

    Args:
        email_address: 邮箱地址
        app_password: 应用专用密码

    Returns:
        EmailService 实例
    """
    return EmailService(email_address, app_password)


# 测试函数
async def test_email_service():
    """测试邮件服务"""
    service = await get_email_service_from_config()

    if not service:
        print("❌ 无法获取邮件服务配置")
        return

    print(f"✓ 邮件服务配置获取成功: {service.email}")

    # 测试 IMAP 连接
    try:
        with service:
            # 测试获取邮件
            print("\n正在检查最近的邮件...")
            emails = service.get_recent_emails(
                folder="INBOX",
                minutes=60,
                limit=5
            )

            print(f"找到 {len(emails)} 封邮件:")
            for mail in emails:
                print(f"  - [{mail['date']}] {mail['subject'][:50]}...")

            # 测试查找 OZON 验证码
            print("\n正在查找 OZON 验证码...")
            code = service.find_ozon_verification_code(minutes=60)

            if code:
                print(f"✓ 找到验证码: {code}")
            else:
                print("未找到最近的 OZON 验证码")

    except Exception as e:
        print(f"❌ 测试失败: {e}")


if __name__ == "__main__":
    asyncio.run(test_email_service())
