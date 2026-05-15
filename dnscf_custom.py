#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloudflare DNS 更新器-自定义
获取优选 IP 并更新 Cloudflare DNS 记录

添加飞书自定义机器人webhook推送，优化通知样式
添加自定义时区功能，默认东八区
"""

import json
import traceback
import time
import os

import requests
import tldextract
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, List

# --- 新增：全局数据结构 ---
class UpdateEntry(NamedTuple):
    domain: str         # 域名
    current_ip: str     # 原始 IP
    ip: str             # 优选 IP
    status: str         # 执行状态（✅ 成功/⏭️ SKIP（配置未变）/❌ 失败）

# 全局变量，用于存储本次运行的所有更新信息
UPDATE_RESULTS: List[UpdateEntry] = []

# 自动注入配置
# 1. 获取触发事件名称
event_name = os.getenv('GITHUB_EVENT_NAME', 'unknown')

# 2. 定义映射关系，将原始变量名转换为可读的中文
trigger_map = {
    'workflow_dispatch': '手动触发',
    'schedule': '定时触发',
    'push': '推送触发',
    'repository_dispatch': 'API外部触发'
}

# 3. 匹配触发类型（如果不在映射中，显示原始名称）
trigger_type = trigger_map.get(event_name, f"其他触发: {event_name}")

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

# API 配置
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")
CF_ZONE_ID = os.environ.get("CF_ZONE_ID")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_DNS_NAME = os.environ.get("CF_DNS_NAME")
FEISHU_CLOUDFLARE_WEBHOOK_URL = os.environ.get("FEISHU_CLOUDFLARE_WEBHOOK_URL")
TIME_OFFSET = get_env_time_offset()
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN")

# 请求头
HEADERS = {
    'Authorization': f'Bearer {CF_API_TOKEN}',
    'Content-Type': 'application/json'
}

# 默认超时时间（秒）
DEFAULT_TIMEOUT = 30

def get_cf_speed_test_ip(timeout=10, max_retries=5):
    """
    获取 Cloudflare 优选 IP

    Args:
        timeout: 单次请求超时时间
        max_retries: 最大重试次数

    Returns:
        优选 IP 字符串，失败返回 None
    """
    for attempt in range(max_retries):
        try:
            response = requests.get(
                'https://ip.164746.xyz/ipTop.html',
                timeout=timeout
            )
            if response.status_code == 200:
                return response.text
        except Exception as e:
            print(f"获取优选 IP 失败 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                traceback.print_exc()
    return None


def get_dns_records(name):
    """
    获取指定名称的 DNS 记录列表（仅 A 类型）

    Args:
        name: DNS 记录名称

    Returns:
        记录字典列表（包含 id 和 content），失败返回空列表
    """
    records = []
    url = f'https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records'

    try:
        response = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 200:
            result = response.json().get('result', [])
            for record in result:
                # 只获取 A 类型记录，避免更新其他类型记录导致 400 错误
                if record.get('name') == name and record.get('type') == 'A':
                    records.append({
                        'id': record['id'],
                        'content': record.get('content', ''),
                    })
        else:
            print(f'获取 DNS 记录失败: {response.text}')
    except Exception as e:
        print(f'获取 DNS 记录异常: {e}')
        traceback.print_exc()

    return records


def update_dns_record(record_info, name, cf_ip):
    """
    更新 DNS 记录

    Args:
        record_info: DNS 记录字典，包含 id 和 content
        name: DNS 记录名称
        cf_ip: 新的 IP 地址

    Returns:
        操作结果字符串
    """
    record_id = record_info['id']
    current_ip = str(record_info.get('content', '')).strip()
    cf_ip = str(cf_ip).strip()

    # 如果 IP 相同，则跳过更新
    if current_ip == cf_ip:
        current_time = f"Local: {get_adjusted_time()['local_24_with_tz']} / UTC: {get_adjusted_time()['utc_str']}"
        print(f"cf_dns_change skip: ---- Time: {current_time} ---- ip：{cf_ip} (配置未变)")
        UPDATE_RESULTS.append(UpdateEntry(domain=name, current_ip=current_ip, ip=cf_ip, status="⏭️ 跳过（配置未变）"))
        return f"ip:{cf_ip} 解析 {name} 跳过 (配置未变)"

    url = f'https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{record_id}'
    data = {
        'type': 'A',
        'name': name,
        'content': cf_ip,
    }

    try:
        response = requests.put(url, headers=HEADERS, json=data, timeout=DEFAULT_TIMEOUT)
        current_time = f"Local: {get_adjusted_time()['local_24_with_tz']} / UTC: {get_adjusted_time()['utc_str']}"

        if response.status_code == 200:
            print(f"cf_dns_change success: ---- Time: {current_time} ---- ip：{cf_ip}")
            UPDATE_RESULTS.append(UpdateEntry(domain=name, current_ip=current_ip, ip=cf_ip, status="✅ 成功"))
            return f"ip:{cf_ip} 解析 {name} 成功"

        raw_text = response.text
        if "identical record already exists" in raw_text.lower():
            msg = f"ip:{cf_ip} 已被同名记录占用，视为更新成功"
            print(f"cf_dns_change success (match): {msg} ---- Time: {current_time}")
            UPDATE_RESULTS.append(UpdateEntry(domain=name, current_ip=current_ip, ip=cf_ip, status="✅ 成功（同名记录占用）"))
            return msg
        else:
            # 排除重复记录报错后的其他真正失败情况
            print(f"cf_dns_change FAIL: ---- Time: {current_time} ---- MESSAGE: {raw_text}")
            UPDATE_RESULTS.append(UpdateEntry(domain=name, current_ip=current_ip, ip=cf_ip, status="❌ 更新失败"))
            return f"ip:{cf_ip} 更新失败"
    except Exception as e:
        traceback.print_exc()
        current_time = f"Local: {get_adjusted_time()['local_24_with_tz']} / UTC: {get_adjusted_time()['utc_str']}"
        print(f"cf_dns_change ERROR: ---- Time: {current_time} ---- MESSAGE: {e}")
        UPDATE_RESULTS.append(UpdateEntry(domain=name, current_ip=current_ip, ip=cf_ip, status="❌ 失败"))
        return f"ip:{cf_ip} 解析 {name} 失败"

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

def get_visual_width(text):
    """简单计算字符串的视觉宽度：中文占2，英文占1"""
    width = 0
    for char in str(text):
        if '\u4e00' <= char <= '\u9fff':
            width += 2
        else:
            width += 1
    return width

def calculate_column_widths(rows, column_keys):
    # 1. 计算每列最大视觉宽度（基础宽度）
    max_widths = {key: get_visual_width(key) for key in column_keys}
    for row in rows:
        for key in column_keys:
            current_val = row.get(key, "")
            max_widths[key] = max(max_widths[key], get_visual_width(current_val))
    
    # 2. 执行状态列保底宽度（视觉宽度至少 8）
    if "status" in max_widths:
        max_widths["status"] = max(max_widths["status"], 8)

    # 3. 转换为 px 单位（直接使用视觉宽度 + 可选内边距）
    # 你可以在这里加固定内边距，比如 + 16 让表格更宽松
    PADDING = 16  # 左右留白，可根据需求调整
    widths_px = {key: f"{width + PADDING}px" for key, width in max_widths.items()}
    
    return widths_px

def get_run_url():
    server_url = os.getenv('GITHUB_SERVER_URL')
    repo = os.getenv('GITHUB_REPOSITORY')
    run_id = os.getenv('GITHUB_RUN_ID')
    
    return f"{server_url}/{repo}/actions/runs/{run_id}"

def get_primary_domain(hostname):
    ext = tldextract.extract(hostname)
    return f"{ext.domain}.{ext.suffix}"

def get_cloudflare_dns_url():
    cf_account = CF_ACCOUNT_ID if CF_ACCOUNT_ID else "CF_ACCOUNT_ID Not Set"
    primary_domain = get_primary_domain(CF_DNS_NAME)

    return f"https://dash.cloudflare.com/{cf_account}/{primary_domain}/dns/records"

def feishu():
    """
    发送 飞书 自定义机器人webhook消息推送
    """
    if not FEISHU_CLOUDFLARE_WEBHOOK_URL:
        print("FEISHU_CLOUDFLARE_WEBHOOK_URL 未设置，跳过飞书消息推送")
        return

    has_error = any("❌" in r.status for r in UPDATE_RESULTS)
    header_template = "red" if has_error else "blue"
    action_type = "danger" if has_error else "primary"

    rows = []
    for r in UPDATE_RESULTS:
        rows.append({
            "customer_name": r.domain,
            "customer_scale": r.current_ip,
            "customer_arr": r.ip,
            "col_5we3lrue2z": r.status
        })

    column_keys = ["domain", "current_ip", "ip", "status"]
    widths = calculate_column_widths(rows, column_keys)
    
    payload = {
        "schema": "2.0",
        "config": {
            "update_multi": True
        },
        "body": {
            "direction": "vertical",
            "horizontal_spacing": "8px",
            "vertical_spacing": "8px",
            "horizontal_align": "center",
            "vertical_align": "center",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {
                    "tag": "table",
                    "columns": [
                        {
                            "data_type": "text",
                            "name": "customer_name",
                            "display_name": "域名",
                            "horizontal_align": "left",
                            "vertical_align": "center",
                            "width": widths["domain"]
                        },
                        {
                            "data_type": "text",
                            "name": "customer_scale",
                            "display_name": "原始 IP",
                            "horizontal_align": "left",
                            "vertical_align": "center",
                            "width": widths["current_ip"]
                        },
                        {
                            "data_type": "text",
                            "name": "customer_arr",
                            "display_name": "优选 IP",
                            "horizontal_align": "left",
                            "vertical_align": "center",
                            "width": widths["ip"]
                        },
                        {
                            "data_type": "text",
                            "name": "col_5we3lrue2z",
                            "display_name": "执行状态",
                            "horizontal_align": "left",
                            "vertical_align": "center",
                            "width": widths["status"]
                        }
                    ],
                    "rows": rows,
                    "row_height": "high",
                    "header_style": {
                        "background_style": "grey",
                        "bold": True,
                        "lines": 3
                    },
                    "page_size": 10,
                    "margin": "0px 0px 0px 0px"
                },
                {
                    "tag": "column_set",
                    "horizontal_spacing": "8px",
                    "horizontal_align": "center",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {
                                        "tag": "plain_text",
                                        "content": "🔗 GitHub Actions详情"
                                    },
                                    "type": action_type,
                                    "width": "default",
                                    "size": "medium",
                                    "behaviors": [
                                        {
                                            "type": "open_url",
                                            "default_url": get_run_url(),
                                            "pc_url": "",
                                            "ios_url": "",
                                            "android_url": ""
                                        }
                                    ],
                                    "margin": "0px 0px 0px 0px"
                                }
                            ],
                            "padding": "0px 0px 0px 0px",
                            "direction": "vertical",
                            "horizontal_spacing": "8px",
                            "vertical_spacing": "8px",
                            "horizontal_align": "center",
                            "vertical_align": "center",
                            "margin": "0px 0px 0px 0px",
                            "weight": 1
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {
                                        "tag": "plain_text",
                                        "content": "🔗 检查 Cloudflare DNS"
                                    },
                                    "type": "primary",
                                    "width": "default",
                                    "size": "medium",
                                    "behaviors": [
                                        {
                                            "type": "open_url",
                                            "default_url": get_cloudflare_dns_url(),
                                            "pc_url": "",
                                            "ios_url": "",
                                            "android_url": ""
                                        }
                                    ],
                                    "margin": "0px 0px 0px 0px"
                                }
                            ],
                            "padding": "0px 0px 0px 0px",
                            "direction": "vertical",
                            "horizontal_spacing": "8px",
                            "vertical_spacing": "8px",
                            "horizontal_align": "center",
                            "vertical_align": "center",
                            "margin": "0px 0px 0px 0px",
                            "weight": 1
                        }
                    ],
                    "margin": "0px 0px 0px 0px"
                },
                {
                    "tag": "hr",
                    "margin": "0px 0px 0px 0px"
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": "执行时间\n🕔 Local [12h] : {get_adjusted_time()['local_12_with_tz']}\n🕔 Local [24h] : {get_adjusted_time()['local_24_with_tz']}\n🌍 UTC : {get_adjusted_time()['utc_str']}",
                        "text_size": "notation",
                        "text_align": "left",
                        "text_color": "grey"
                    },
                    "icon": {
                        "tag": "standard_icon",
                        "token": "lark-logo_colorful",
                        "color": "light_grey"
                    }
                }
            ]
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🌐 Cloudflare优选IP 更新结果（{trigger_type}）"
            },
            "subtitle": {
                "tag": "plain_text",
                "content": ""
            },
            "template": header_template,
            "padding": "12px 12px 12px 12px"
        }
    }

    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.post(FEISHU_CLOUDFLARE_WEBHOOK_URL, json=payload, timeout=DEFAULT_TIMEOUT)
        if response.status_code != 200:
            print(f"飞书推送返回错误: {response.text}")
    except Exception as e:
        print(f"飞书消息推送失败: {e}")

def push_plus(content):
    """
    发送 PushPlus 消息推送

    Args:
        content: 消息内容
    """
    if not PUSHPLUS_TOKEN:
        print("PUSHPLUS_TOKEN 未设置，跳过消息推送")
        return

    url = 'http://www.pushplus.plus/send'
    data = {
        "token": PUSHPLUS_TOKEN,
        "title": "IP优选DNSCF推送",
        "content": content,
        "template": "markdown",
        "channel": "wechat"
    }

    try:
        body = json.dumps(data).encode(encoding='utf-8')
        headers = {'Content-Type': 'application/json'}
        requests.post(url, data=body, headers=headers, timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        print(f"消息推送失败: {e}")


def main():
    """主函数"""
    # 检查必要的环境变量
    if not all([CF_API_TOKEN, CF_ZONE_ID, CF_DNS_NAME]):
        print("错误: 缺少必要的环境变量 (CF_API_TOKEN, CF_ZONE_ID, CF_DNS_NAME)")
        return

    # 获取最新优选 IP
    ip_addresses_str = get_cf_speed_test_ip()
    if not ip_addresses_str:
        print("错误: 无法获取优选 IP")
        return

    ip_addresses = [ip.strip() for ip in ip_addresses_str.split(',') if ip.strip()]
    if not ip_addresses:
        print("错误: 未解析到有效 IP 地址")
        return

    # 获取 DNS 记录
    dns_records = get_dns_records(CF_DNS_NAME)
    if not dns_records:
        print(f"错误: 未找到 DNS 记录")
        return

    # --- 新增：按 content 排序，确保每次更新的顺序一致 ---
    dns_records.sort(key=lambda x: x['content'])
    ip_addresses.sort()

    # 检查记录数量是否足够
    if len(ip_addresses) > len(dns_records):
        print(f"警告: IP 数量({len(ip_addresses)})超过 DNS 记录数量({len(dns_records)})，只更新前 {len(dns_records)} 个")
        ip_addresses = ip_addresses[:len(dns_records)]

    # 更新 DNS 记录
    push_plus_content = []
    for index, ip_address in enumerate(ip_addresses):
        dns = update_dns_record(dns_records[index], CF_DNS_NAME, ip_address)
        push_plus_content.append(dns)

    # 发送推送
    if push_plus_content:
        full_content = '\n'.join(push_plus_content)
        push_plus(full_content)
    if UPDATE_RESULTS:
        feishu()


if __name__ == '__main__':
    main()
