#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域名过期检查
检查指定域名是否过期，达到预设过期时通过飞书通知
"""

import os
import rdap
import whois
import requests
import tldextract
from datetime import datetime, timedelta, timezone

# 默认超时时间（秒）
DEFAULT_TIMEOUT = 30

def get_env_time_offset():
    """
    获取并校验时区偏移量（默认为东八区）
    """
    raw_offset = os.environ.get("TIME_OFFSET", "8")

    if not raw_offset:
        return 8.0
    try:
        offset = float(raw_offset)
        if -12 <= offset <= 14:
            return offset
        else:
            print(f"警告: TIME_OFFSET ({offset}) 超出常规范围 [-12, 14]，将使用 UTC+0")
            return 0
    except ValueError:
        print(f"错误: TIME_OFFSET '{raw_offset}' 不是有效的数字，将使用 UTC+8")
        return 8.0

# 配置
CF_DNS_NAME = os.environ.get("CF_DNS_NAME")
FEISHU_DOMAIN_WEBHOOK_URL = os.environ.get("FEISHU_DOMAIN_WEBHOOK_URL")
DOMAIN_WARN_DAYS = int(os.environ.get("DOMAIN_WARN_DAYS", 10))
TIME_OFFSET = get_env_time_offset()

def get_primary_domain(hostname):
    ext = tldextract.extract(hostname)
    return f"{ext.domain}.{ext.suffix}"

def get_domain_info_rdap(domain):
    try:
        client = rdap.RdapClient(timeout=DEFAULT_TIMEOUT)
        obj = client.get_domain(domain)
        expire_date = None
        registrar_name = None
        registrar_url = None

        # 1. 解析过期时间（从 events 中）
        if hasattr(obj, 'events'):
            for event in obj.events:
                if event.eventAction == 'expiration':
                    # 直接解析为带时区的 datetime 对象
                    expire_date = datetime.fromisoformat(event.eventDate.replace('Z', '+00:00'))
                    break

        # 2. 解析注册商信息
        if hasattr(obj, 'entities'):
            for ent in obj.entities:
                if "registrar" in getattr(ent, 'roles', []):
                    if hasattr(ent, 'vcardArray') and ent.vcardArray:
                        for v in ent.vcardArray[1]:
                            if v[0] == 'fn':
                                registrar_name = v[3]
                    if hasattr(ent, 'links'):
                        for link in ent.links:
                            if getattr(link, 'rel', None) == 'about' and hasattr(link, 'href'):
                                registrar_url = link.href
                    break
        return True, expire_date, registrar_name, registrar_url
    except Exception as e:
        print("RDAP异常:", e)
        return False, None, None, None

def get_domain_info_whois(domain):
    """WHOIS 查询（降级）"""
    try:
        w = whois.whois(domain)
        exp = w.expiration_date
        if isinstance(exp, list):
            exp = exp[0]
        registrar_name = w.registrar if hasattr(w, 'registrar') else None
        registrar_url = w.registrar_url if hasattr(w, 'registrar_url') else None
        return True, exp, registrar_name, registrar_url
    except Exception as e:
        print("WHOIS异常:", e)
        return False, None, None, None

def check_domain(domain):
    # 1.RDAP
    ok, exp, reg_name, reg_url = get_domain_info_rdap(domain)
    if ok and exp:
        return exp, reg_name, reg_url, "RDAP"
    # 2.WHOIS
    ok, exp, reg_name, reg_url = get_domain_info_whois(domain)
    if ok and exp:
        return exp, reg_name, reg_url, "WHOIS"
    return None, None, None, "失败"

def get_adjusted_time():
    now_utc = datetime.now(timezone.utc)
    adjusted_now = now_utc + timedelta(hours=TIME_OFFSET)

    # 本地 24小时制
    local_24 = adjusted_now.strftime('%Y-%m-%d %H:%M:%S')
    local_24_with_tz = f"{local_24} (UTC+{int(TIME_OFFSET)})"
    # 本地 12小时制 AM/PM
    local_12 = adjusted_now.strftime('%Y-%m-%d %I:%M:%S %p')
    local_12_with_tz = f"{local_12} (UTC+{int(TIME_OFFSET)})"
    # UTC 24小时制
    utc_str = now_utc.strftime('%Y-%m-%d %H:%M:%S')

    return {
        "local_24": local_24,
        "local_24_with_tz": local_24_with_tz,
        "local_12": local_12,
        "local_12_with_tz": local_12_with_tz,
        "utc_str": utc_str
    }

def feishu(domain, exp_ms, days_left, reg_name, reg_url):
    """
    发送 飞书 自定义机器人webhook消息推送
    """
    if not FEISHU_DOMAIN_WEBHOOK_URL:
        print("FEISHU_DOMAIN_WEBHOOK_URL 未设置，跳过飞书消息推送")
        return

    btn_text = reg_name if reg_name else "暂无域名注册商信息"
    btn_url = reg_url if (reg_url and reg_url.startswith('http')) else "https://open.feishu.cn/404"

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "template": "red",
                "title": {
                    "tag": "plain_text",
                    "content": f"⚠️ 域名{domain}即将过期"
                }
            },
            "elements": [
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "horizontal_align": "center",
                    "background_style": "grey",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "div",
                                    "text": {
                                        "tag": "lark_md",
                                        "content": f"**过期日期**\n<local_datetime millisecond=\"{exp_ms}\" format_type=\"date_time\"></local_datetime>",
                                        "text_align": "center"
                                    }
                                }
                            ]
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "div",
                                    "text": {
                                        "tag": "lark_md",
                                        "content": f"**剩余天数**\n<font color='orange'>{days_left}</font>",
                                        "text_align": "center"
                                    }
                                }
                            ]
                        }
                    ]
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": f"🔗 {btn_text}"
                            },
                            "type": "primary",
                            "url": btn_url
                        }
                    ]
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "执行时间\n"
                            f"🕔 Local: {get_adjusted_time()['local_12_with_tz']}\n"
                            f"🕔 Local: {get_adjusted_time()['local_24_with_tz']}\n"
                            f"🌍 UTC: {get_adjusted_time()['utc_str']}"
                        )
                    }
                }
            ]
        }
    }

    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.post(FEISHU_DOMAIN_WEBHOOK_URL, json=payload, timeout=DEFAULT_TIMEOUT)
        if response.status_code != 200:
            print(f"飞书推送返回错误: {response.text}")
    except Exception as e:
        print(f"飞书消息推送失败: {e}")

def main():
    print(f"预警天数：{DOMAIN_WARN_DAYS}")
    primary_domain = get_primary_domain(CF_DNS_NAME)
    exp_date, reg_name, reg_url, source = check_domain(primary_domain)
    print(f"查询方式：{source}")

    if not exp_date:
        print("查询失败")
        return
    else:
        print("查询成功")

    print(f"注册商：{reg_name}，注册商URL：{reg_url}")
    
    # ---------------- 修复时区问题 ----------------
    # 统一使用 UTC 时间，并确保都是带时区的 datetime 对象
    now = datetime.now(timezone.utc)
    # 如果 exp_date 没有时区信息，则强制设为 UTC
    if exp_date.tzinfo is None or exp_date.utcoffset() is None:
        exp_date = exp_date.replace(tzinfo=timezone.utc)
    
    days_left = (exp_date - now).days
    exp_ms = int(exp_date.timestamp() * 1000)
    print(f"剩余天数：{days_left}")

    if days_left < DOMAIN_WARN_DAYS:
        print("即将过期，开始预警")
        feishu(primary_domain, exp_ms, days_left, reg_name, reg_url)
    else:
        print("尚未过期")

if __name__ == "__main__":
    main()