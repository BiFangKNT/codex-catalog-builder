#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import copy
import shutil
import tomllib
import requests
from pathlib import Path

# ==================== 1. 路径定义 ====================
CONFIG_PATH = Path.home() / ".codex" / "config.toml"
CACHE_PATH = Path.home() / ".codex" / "models_cache.json"
CATALOG_PATH = Path.home() / ".codex" / "custom_catalog.json"


# ==================== 2. 动态读取 config.toml ====================
def load_proxy_config():
    """从 ~/.codex/config.toml 中动态读取 model_provider 及其 base_url、bearer_token"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"未找到 Codex 配置文件: {CONFIG_PATH}")

    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        raise RuntimeError(f"解析 {CONFIG_PATH} 失败: {e}")

    # 1. 读取顶层 model_provider
    model_provider = cfg.get("model_provider")
    if not model_provider:
        raise ValueError(f"{CONFIG_PATH} 中缺少顶层字段 'model_provider'！")

    # 2. 读取 [model_providers.$model_provider]
    model_providers = cfg.get("model_providers", {})
    provider_cfg = model_providers.get(model_provider)
    if not provider_cfg:
        raise ValueError(f"{CONFIG_PATH} 中未找到 [model_providers.{model_provider}] 配置段！")

    base_url = provider_cfg.get("base_url")
    if not base_url:
        raise ValueError(f"[model_providers.{model_provider}] 中缺少 'base_url' 字段！")

    bearer_token = provider_cfg.get("experimental_bearer_token", "")

    return base_url, bearer_token, model_provider


# ==================== 3. 跨平台按键捕获 ====================
if os.name == 'nt':
    import msvcrt
    def get_key():
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):
            ch2 = msvcrt.getch()
            if ch2 == b'H': return 'UP'
            if ch2 == b'P': return 'DOWN'
            return 'OTHER'
        if ch == b'\r': return 'ENTER'
        if ch == b'\x1b': return 'ESC'
        if ch == b' ': return 'SPACE'
        try:
            return ch.decode('utf-8', errors='ignore')
        except:
            return ''
else:
    import termios
    import tty
    import select
    def get_key():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                r, _, _ = select.select([sys.stdin], [], [], 0.08)
                if r:
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        ch3 = sys.stdin.read(1)
                        if ch3 == 'A': return 'UP'
                        if ch3 == 'B': return 'DOWN'
                    return 'OTHER'
                return 'ESC'
            if ch in ('\r', '\n'): return 'ENTER'
            if ch == ' ': return 'SPACE'
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# ==================== 4. 辅助函数 ====================
def parse_token_input(val_str: str, default_val: int):
    """解析输入的数值，支持 200k / 1m 等简写"""
    s = val_str.strip().lower()
    if not s:
        return default_val
    try:
        if s.endswith('k'):
            return int(float(s[:-1]) * 1000)
        elif s.endswith('m'):
            return int(float(s[:-1]) * 1000000)
        else:
            return int(s)
    except ValueError:
        return default_val


def parse_range_indices(input_str: str, max_len: int):
    """解析如 1, 3, 5-8, 10~12 的范围字符串，返回 0-based 索引列表"""
    res = set()
    parts = [p.strip() for p in input_str.replace('，', ',').split(',') if p.strip()]
    for part in parts:
        part = part.replace('~', '-')
        if '-' in part:
            sub = part.split('-')
            if len(sub) == 2 and sub[0].strip().isdigit() and sub[1].strip().isdigit():
                start, end = int(sub[0].strip()), int(sub[1].strip())
                if start > end:
                    start, end = end, start
                for i in range(start, end + 1):
                    if 1 <= i <= max_len:
                        res.add(i - 1)
        elif part.isdigit():
            idx = int(part)
            if 1 <= idx <= max_len:
                res.add(idx - 1)
    return sorted(list(res))


# ==================== 5. 核心 TUI 状态管理器 ====================
class CodexCatalogApp:
    def __init__(self):
        # 1. 动态加载配置
        try:
            self.proxy_base, self.proxy_key, self.provider_name = load_proxy_config()
        except Exception as e:
            print(f"❌ 配置读取错误: {e}")
            sys.exit(1)

        self.items = []
        self.cursor_idx = 0
        self.template_model = None
        self.default_context = 272000

        # 2. 加载模板并获取模型
        self.load_template()
        self.fetch_models()

    def load_template(self):
        """优先提取 slug 为 gpt-5.4 的模板"""
        if CACHE_PATH.exists():
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    raw = data.get("models") or list(data.values())
                    for m in raw:
                        if isinstance(m, dict) and m.get("slug") == "gpt-5.4":
                            self.template_model = m
                            break
                    if not self.template_model:
                        for m in raw:
                            if isinstance(m, dict) and "slug" in m:
                                self.template_model = m
                                break
            except Exception as e:
                print(f"读取本地 models_cache 失败: {e}")

        if not self.template_model:
            self.template_model = {
                "slug": "gpt-5.4",
                "display_name": "gpt-5.4",
                "description": "Fallback gpt-5.4 template",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 0,
                "context_window": 272000,
                "effective_context_window_percent": 0.95,
                "shell_type": "shell_command",
                "input_modalities": ["text", "image"],
                "supported_reasoning_levels": [{"effort": "none", "description": "off"}],
                "default_reasoning_level": "none"
            }
        
        self.default_context = self.template_model.get("context_window", 272000)

    def fetch_models(self):
        """从代理 API 获取模型列表"""
        url = f"{self.proxy_base.rstrip('/')}/models"
        headers = {}
        if self.proxy_key:
            headers["Authorization"] = f"Bearer {self.proxy_key}"

        print(f"正在从 [{self.provider_name}] ({url}) 获取模型列表 ...")
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            for item in data:
                m_id = item["id"]
                self.items.append({
                    "id": m_id,
                    "checked": False,
                    "custom_context": None
                })
        except Exception as e:
            print(f"❌ 获取模型失败: {e}")
            sys.exit(1)

        if not self.items:
            print("❌ 代理未返回任何可用模型！")
            sys.exit(1)

    def render_list(self, edit_mode=False):
        """绘制终端界面"""
        term_height = shutil.get_terminal_size().lines
        max_display = max(8, term_height - 10)
        
        scroll_top = max(0, min(self.cursor_idx - max_display // 2, len(self.items) - max_display))
        scroll_bottom = min(len(self.items), scroll_top + max_display)

        sys.stdout.write("\033[H\033[J")
        print(f"📦 Codex Model Catalog 配置器 | Provider: \033[1;32m{self.provider_name}\033[0m | 默认: gpt-5.4 ({self.default_context:,} tokens)")
        print("=" * 75)

        for idx in range(scroll_top, scroll_bottom):
            item = self.items[idx]
            check_mark = "[x]" if item["checked"] else "[ ]"
            
            if item["custom_context"] is not None:
                val_str = f"{item['custom_context']:,} tokens"
            elif item["checked"]:
                val_str = "待输入配置"
            else:
                val_str = f"(默认: {self.default_context:,})"

            line = f"{check_mark} {idx + 1:2d}. {item['id']:<42} Context: {val_str}"

            if edit_mode and idx == self.cursor_idx:
                sys.stdout.write(f"\033[1;36;7m> {line:<71}\033[0m\n")
            else:
                sys.stdout.write(f"  {line}\n")

        print("=" * 75)

    def prompt_for_item_context(self, idx: int, current_step: int, total_steps: int):
        """为单个选中项配置 context_window"""
        item = self.items[idx]
        cur_val = item["custom_context"] or self.default_context
        prompt = (
            f"\n配置 [{current_step}/{total_steps}] 模型 \033[1;33m{item['id']}\033[0m 的 context_window\n"
            f"(当前值: {cur_val:,}，支持输入简写如 200k/128k/1m，直接回车保持): "
        )
        val_in = input(prompt).strip()
        if val_in:
            parsed = parse_token_input(val_in, cur_val)
            item["custom_context"] = parsed
        elif item["custom_context"] is None:
            item["custom_context"] = cur_val

    def run_edit_mode(self):
        orig_states = [(it["checked"], it["custom_context"]) for it in self.items]
        
        while True:
            self.render_list(edit_mode=True)
            print("【编辑模式】[↑/↓]移动光标 | [空格]选中/取消 | [a]全选/全消 | [Enter]确定配置 | [ESC]取消")
            
            key = get_key()
            if key == 'UP':
                self.cursor_idx = (self.cursor_idx - 1) % len(self.items)
            elif key == 'DOWN':
                self.cursor_idx = (self.cursor_idx + 1) % len(self.items)
            elif key == 'SPACE':
                self.items[self.cursor_idx]["checked"] = not self.items[self.cursor_idx]["checked"]
            elif key in ('a', 'A'):
                all_checked = all(it["checked"] for it in self.items)
                for it in self.items:
                    it["checked"] = not all_checked
            elif key == 'ESC':
                for i, (chk, ctx) in enumerate(orig_states):
                    self.items[i]["checked"] = chk
                    self.items[i]["custom_context"] = ctx
                break
            elif key == 'ENTER':
                selected_indices = [i for i, it in enumerate(self.items) if it["checked"]]
                if selected_indices:
                    for step, idx in enumerate(selected_indices, 1):
                        self.prompt_for_item_context(idx, step, len(selected_indices))
                for i, it in enumerate(self.items):
                    if not it["checked"]:
                        it["custom_context"] = None
                break

    def run_select_mode(self):
        self.render_list(edit_mode=False)
        print("【选中模式】请输入要选中的序号 (例如: 1, 3, 5-8 或 10~12，留空/ESC返回)")
        val_in = input("序号: ").strip()
        if not val_in:
            return

        indices = parse_range_indices(val_in, len(self.items))
        if not indices:
            print("⚠️ 没有匹配的有效序号！")
            input("按回车继续..."); return

        for idx in indices:
            self.items[idx]["checked"] = True

        for step, idx in enumerate(indices, 1):
            self.prompt_for_item_context(idx, step, len(indices))

    def run_deselect_mode(self):
        self.render_list(edit_mode=False)
        print("【取消选中模式】请输入要取消的序号 (例如: 1, 3, 5-8 或 10~12，留空/ESC返回)")
        val_in = input("序号: ").strip()
        if not val_in:
            return

        indices = parse_range_indices(val_in, len(self.items))
        for idx in indices:
            self.items[idx]["checked"] = False
            self.items[idx]["custom_context"] = None

    def apply_and_save(self):
        generated_models = []
        for it in self.items:
            m_id = it["id"]
            entry = copy.deepcopy(self.template_model)
            entry["slug"] = m_id
            entry["display_name"] = m_id
            entry["visibility"] = "list"
            entry["supported_in_api"] = True
            entry["priority"] = 0

            if it["checked"] and it["custom_context"] is not None:
                entry["context_window"] = it["custom_context"]
            else:
                entry["context_window"] = self.default_context

            generated_models.append(entry)

        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CATALOG_PATH, "w", encoding="utf-8") as f:
            json.dump({"models": generated_models}, f, indent=2, ensure_ascii=False)

        print("\n" + "=" * 75)
        print(f"🎉 成功生成 {len(generated_models)} 个模型配置！")
        print(f"📁 已写入至: {CATALOG_PATH}")
        print("=" * 75)
        sys.exit(0)

    def run(self):
        while True:
            self.render_list(edit_mode=False)
            print("【主菜单】[1] 编辑  [2] 选中  [3] 取消选中  [4] 应用并退出")
            sys.stdout.write("请按数字键选择 (1-4): ")
            sys.stdout.flush()

            key = get_key()
            if key == '1':
                self.run_edit_mode()
            elif key == '2':
                self.run_select_mode()
            elif key == '3':
                self.run_deselect_mode()
            elif key == '4':
                self.apply_and_save()


def main():
    app = CodexCatalogApp()
    app.run()


if __name__ == "__main__":
    main()