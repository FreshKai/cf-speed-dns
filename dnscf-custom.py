#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloudflare DNS 更新器-自定义
获取优选 IP 并更新 Cloudflare DNS 记录

添加控制代理状态，当CF_PPOXY_STATUS设为true时为橙色云，为false或不设置时为灰色云
添加飞书自定义机器人webhook推送
IP相同且代理状态相同，跳过更新
添加自定义时区功能，默认东八区
"""

import json
import traceback
import time
import os

import requests
from datetime import datetime, timedelta

def get_env_time_offset():
    """
    获取并校验时区偏移量（默认为东八区）
    """
    raw_offset = os.environ.get("TIME_OFFSET", "8")
    try:
        offset = float(raw_offset)
        if -12 <= offset <= 14:
            return offset
        else:
            print(f"警告: TIME_OFFSET ({offset}) 超出常规范围 [-12, 14]，将使用 UTC+0")
            return 0
    except ValueError:
        print(f"错误: TIME_OFFSET '{raw_offset}' 不是有效的数字，将使用 UTC+0")
        return 0

# API 配置
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")
CF_ZONE_ID = os.environ.get("CF_ZONE_ID")
CF_DNS_NAME = os.environ.get("CF_DNS_NAME")
CF_PROXY_STATUS = os.environ.get("CF_PROXY_STATUS")
FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL")
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
                        'proxied': record.get('proxied', False)
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
    current_ip = record_info.get('content', '')
    current_proxied = record_info.get('proxied', False)

    is_proxied = str(CF_PROXY_STATUS).lower() == 'true'
    status_str = "橙色云" if is_proxied else "灰色云"

    # 如果 IP 相同 且 代理状态也相同，则跳过更新
    if current_ip == cf_ip and current_proxied == is_proxied:
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"cf_dns_change skip: [{status_str}] ---- Time: {current_time} ---- ip：{cf_ip} (配置未变)")
        return f"[{status_str}] ip:{cf_ip} 解析 {name} 跳过 (配置未变)"

    url = f'https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{record_id}'
    data = {
        'type': 'A',
        'name': name,
        'content': cf_ip,
        'proxied': is_proxied
    }

    try:
        response = requests.put(url, headers=HEADERS, json=data, timeout=DEFAULT_TIMEOUT)
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        if response.status_code == 200:
            print(f"cf_dns_change success: [{status_str}] ---- Time: {current_time} ---- ip：{cf_ip}")
            return f"[{status_str}] ip:{cf_ip} 解析 {name} 成功"
        else:
            print(f"cf_dns_change ERROR: [{status_str}] ---- Time: {current_time} ---- MESSAGE: {response.text}")
            return f"[{status_str}] ip:{cf_ip} 解析 {name} 失败"
    except Exception as e:
        traceback.print_exc()
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"cf_dns_change ERROR: [{status_str}] ---- Time: {current_time} ---- MESSAGE: {e}")
        return f"[{status_str}] ip:{cf_ip} 解析 {name} 失败"

def get_adjusted_time():
    # 获取当前 UTC 时间，并根据偏移量进行调整
    adjusted_now = datetime.utcnow() + timedelta(hours=TIME_OFFSET)
    return adjusted_now.strftime('%Y-%m-%d %H:%M:%S')

def feishu(content):
    """
    发送 飞书 自定义机器人webhook消息推送

    Args:
        content: 消息内容
    """
    if not FEISHU_WEBHOOK_URL:
        print("FEISHU_WEBHOOK_URL 未设置，跳过飞书消息推送")
        return

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": "🛡️ Cloudflare优选IP 更新结果"
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**执行详情：**\n{content}"
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"执行时间: {get_adjusted_time()} (UTC{'+' if TIME_OFFSET>=0 else ''}{TIME_OFFSET})"
                        }
                    ]
                }
            ]
        }
    }

    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=DEFAULT_TIMEOUT)
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
        print(f"错误: 未找到 {CF_DNS_NAME} 的 DNS 记录")
        return

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
        feishu(full_content)


if __name__ == '__main__':
    main()
