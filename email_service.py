"""
OZON 登录邮件服务

使用 QQ/163 邮箱 IMAP 来接收验证码邮件
仅支持国内邮箱（QQ、163），不支持 Gmail
"""

import smtplib
import imaplib
import email
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import re
import logging
import asyncio

logger = logging.getLogger(__name__)


# 邮箱服务器配置（仅支持国内邮箱）
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
    """根据邮箱地址检测邮箱服务商（仅支持 QQ、163）"""
    email_lower = email.lower()
    if '@qq.com' in email_lower:
        return 'qq'
    elif '@163.com' in email_lower or '@126.com' in email_lower:
        return '163'
    else:
        raise ValueError(f"不支持的邮箱类型: {email}，仅支持 QQ 邮箱和 163 邮箱")


class EmailService:
    """邮件服务类（支持 QQ、163 邮箱）"""

    def __init__(self, email: str, app_password: str):
        """
        初始化邮件服务

        Args:
            email: 邮箱地址（支持 Gmail、QQ、163）
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

    # 兼容旧代码
    @property
    def gmail(self):
        return self.email

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
            logger.info(f"IMAP 连接成功: {self.email} ({self._provider})")
        return self._imap_conn

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
            msg['From'] = self.gmail
            msg['To'] = to
            msg['Subject'] = subject

            content_type = 'html' if html else 'plain'
            msg.attach(MIMEText(body, content_type, 'utf-8'))

            smtp = self.connect_smtp()
            smtp.sendmail(self.gmail, to, msg.as_string())

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
            imap.select(folder)

            # 搜索最近的邮件
            since_date = (datetime.now() - timedelta(minutes=minutes)).strftime("%d-%b-%Y")
            search_criteria = f'(SINCE "{since_date}")'

            if sender_filter:
                search_criteria = f'(FROM "{sender_filter}" SINCE "{since_date}")'

            _, message_numbers = imap.search(None, search_criteria)

            if not message_numbers[0]:
                return emails

            # 获取邮件ID列表（最新的在前）
            email_ids = message_numbers[0].split()[-limit:]
            email_ids.reverse()

            for email_id in email_ids:
                _, msg_data = imap.fetch(email_id, "(RFC822)")

                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])

                        # 解析发件人
                        from_header = msg.get("From", "")

                        # 解析主题
                        subject = ""
                        subject_header = msg.get("Subject", "")
                        if subject_header:
                            decoded = decode_header(subject_header)
                            subject = "".join(
                                part.decode(encoding or 'utf-8') if isinstance(part, bytes) else part
                                for part, encoding in decoded
                            )

                        # 主题过滤
                        if subject_filter and subject_filter.lower() not in subject.lower():
                            continue

                        # 解析日期
                        date_header = msg.get("Date", "")

                        # 解析正文
                        body = self._get_email_body(msg)

                        emails.append({
                            "id": email_id.decode(),
                            "from": from_header,
                            "subject": subject,
                            "date": date_header,
                            "body": body
                        })

            return emails

        except Exception as e:
            logger.error(f"获取邮件失败: {e}")
            return emails

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
        folders = ["INBOX"]
        if check_spam:
            folders.append("Junk")  # QQ邮箱垃圾文件夹

        for folder in folders:
            try:
                emails = self.get_recent_emails(
                    folder=folder,
                    sender_filter="ozon",
                    minutes=minutes,
                    limit=5
                )

                for mail in emails:
                    code = self._extract_ozon_code(mail["body"], mail["subject"])
                    if code:
                        logger.info(f"找到 OZON 验证码: {code} (来自 {folder})")
                        return code

            except Exception as e:
                logger.warning(f"检查文件夹 {folder} 失败: {e}")
                continue

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

            gmail = config.get("gmail", "")
            app_password = config.get("app_password", "")

            if not gmail or not app_password:
                logger.warning("Gmail 或应用密码未配置")
                return None

            return EmailService(gmail, app_password)

    except Exception as e:
        logger.error(f"获取邮件服务配置失败: {e}")
        return None


def get_email_service_sync(gmail: str, app_password: str) -> EmailService:
    """
    直接创建邮件服务实例（同步版本）

    Args:
        gmail: Gmail 邮箱地址
        app_password: 应用专用密码

    Returns:
        EmailService 实例
    """
    return EmailService(gmail, app_password)


# 测试函数
async def test_email_service():
    """测试邮件服务"""
    service = await get_email_service_from_config()

    if not service:
        print("❌ 无法获取邮件服务配置")
        return

    print(f"✓ 邮件服务配置获取成功: {service.gmail}")

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
