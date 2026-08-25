#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import copy
import shutil
import tomllib
from typing import Any
import requests
from pathlib import Path

# ==================== 1. 路径定义 ====================
CONFIG_PATH = Path.home() / ".codex" / "config.toml"
CACHE_PATH = Path.home() / ".codex" / "models_cache.json"
CATALOG_PATH = Path.home() / ".codex" / "custom_catalog.json"


# ==================== 2. 动态读取 config.toml ====================
def load_proxy_config() -> tuple[str, str, str]:
    """从 ~/.codex/config.toml 中动态读取 model_provider 及其 base_url、bearer_token"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"未找到 Codex 配置文件: {CONFIG_PATH}")

    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        raise RuntimeError(f"解析 {CONFIG_PATH} 失败: {e}")

    model_provider = cfg.get("model_provider")
    if not model_provider or not isinstance(model_provider, str):
        raise ValueError(f"{CONFIG_PATH} 中缺少顶层字符串字段 'model_provider'！")

    model_providers = cfg.get("model_providers", {})
    if not isinstance(model_providers, dict):
        raise ValueError(f"{CONFIG_PATH} 中的 'model_providers' 不是合法的配置段！")

    provider_cfg = model_providers.get(model_provider)
    if not isinstance(provider_cfg, dict):
        raise ValueError(f"{CONFIG_PATH} 中未找到 [model_providers.{model_provider}] 配置段！")

    base_url = provider_cfg.get("base_url")
    if not base_url or not isinstance(base_url, str):
        raise ValueError(f"[model_providers.{model_provider}] 中缺少 'base_url' 字段！")

    bearer_token = str(provider_cfg.get("experimental_bearer_token", ""))
    return base_url, bearer_token, model_provider


# ==================== 3. 增强型跨平台按键与输入捕获 ====================
if os.name == 'nt':
    import msvcrt
    def get_key() -> str:
        ch = msvcrt.getch()
        if ch == b'\x03':  # Ctrl+C 强制退出
            raise KeyboardInterrupt
        if ch in (b'\x00', b'\xe0'):
            ch2 = msvcrt.getch()
            if ch2 == b'H': return 'UP'
            if ch2 == b'P': return 'DOWN'
            return 'OTHER'
        if ch in (b'\r', b'\n'): return 'ENTER'
        if ch == b'\x1b': return 'ESC'
        if ch == b' ': return 'SPACE'
        if ch in (b'\x08', b'\x7f'): return 'BACKSPACE'
        try:
            return ch.decode('utf-8', errors='ignore')
        except Exception:
            return ''
else:
    import termios
    import tty
    import select
    def get_key() -> str:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x03':  # Ctrl+C 强制退出
                raise KeyboardInterrupt
            if ch == '\x1b':
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
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
            if ch in ('\x7f', '\x08'): return 'BACKSPACE'
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def custom_input(prompt: str) -> str | None:
    """
    替换标准 input()，支持：
    - 按 ESC 随时取消操作并返回 None
    - 按 Ctrl+C 立即退出
    - 支持退格删除 (Backspace) 与回车提交
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf: list[str] = []

    while True:
        k = get_key()
        if k == 'ESC':
            sys.stdout.write("\n\033[90m[已按 ESC 取消操作]\033[0m\n")
            return None
        elif k == 'ENTER':
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(buf).strip()
        elif k == 'BACKSPACE':
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
        elif len(k) == 1 and k.isprintable():
            buf.append(k)
            sys.stdout.write(k)
            sys.stdout.flush()


# ==================== 4. 辅助函数 ====================
def parse_token_input(val_str: str, default_val: int) -> int:
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


def parse_range_indices(input_str: str, max_len: int) -> list[int]:
    """解析如 1, 3, 5-8, 10~12 的范围字符串，返回 0-based 索引列表"""
    res: set[int] = set()
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
        try:
            self.proxy_base, self.proxy_key, self.provider_name = load_proxy_config()
        except Exception as e:
            print(f"❌ 配置读取错误: {e}")
            sys.exit(1)

        self.items: list[dict[str, Any]] = []
        self.cursor_idx: int = 0
        self.template_model: dict[str, Any] = {}
        self.default_context: int = 272000

        self.load_template()
        self.fetch_models()

    def load_template(self):
        """优先提取 slug 为 gpt-5.4 的模板"""
        found_template: dict[str, Any] | None = None
        if CACHE_PATH.exists():
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    raw = data.get("models") or list(data.values()) if isinstance(data, dict) else []
                    for m in raw:
                        if isinstance(m, dict) and m.get("slug") == "gpt-5.4":
                            found_template = m
                            break
                    if not found_template:
                        for m in raw:
                            if isinstance(m, dict) and "slug" in m:
                                found_template = m
                                break
            except Exception as e:
                print(f"读取本地 models_cache 失败: {e}")

        if not found_template:
            found_template = {
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
        
        self.template_model = found_template
        self.default_context = int(self.template_model.get("context_window", 272000))

    def fetch_models(self):
        """从代理 API 获取模型列表"""
        url = f"{self.proxy_base.rstrip('/')}/models"
        headers: dict[str, str] = {}
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

    def render_list(self, edit_mode: bool = False):
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

    def prompt_for_item_context(self, idx: int, current_step: int, total_steps: int) -> bool:
        """为单个选中项配置 context_window。按 ESC 返回 False 中断"""
        item = self.items[idx]
        cur_val = item["custom_context"] or self.default_context
        prompt = (
            f"\n配置 [{current_step}/{total_steps}] 模型 \033[1;33m{item['id']}\033[0m 的 context_window\n"
            f"(当前: {cur_val:,}，支持 200k/128k/1m，直接回车保持，按 ESC 放弃本次配置): "
        )
        val_in = custom_input(prompt)
        if val_in is None:
            return False  # 用户按了 ESC
        if val_in:
            parsed = parse_token_input(val_in, cur_val)
            item["custom_context"] = parsed
        elif item["custom_context"] is None:
            item["custom_context"] = cur_val
        return True

    def run_edit_mode(self):
        orig_states = [(it["checked"], it["custom_context"]) for it in self.items]
        
        while True:
            self.render_list(edit_mode=True)
            print("【编辑模式】[↑/↓]移动光标 | [空格]选中/取消 | [a]全选/全消 | [Enter]确定配置 | [ESC]取消返回")
            
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
                    canceled = False
                    for step, idx in enumerate(selected_indices, 1):
                        ok = self.prompt_for_item_context(idx, step, len(selected_indices))
                        if not ok:
                            for i, (chk, ctx) in enumerate(orig_states):
                                self.items[i]["checked"] = chk
                                self.items[i]["custom_context"] = ctx
                            canceled = True
                            break
                    if canceled:
                        break

                for i, it in enumerate(self.items):
                    if not it["checked"]:
                        it["custom_context"] = None
                break

    def run_select_mode(self):
        self.render_list(edit_mode=False)
        print("【选中模式】请输入要选中的序号 (例如: 1, 3, 5-8 或 10~12，按 ESC 取消返回)")
        val_in = custom_input("序号: ")
        if val_in is None or not val_in:
            return

        indices = parse_range_indices(val_in, len(self.items))
        if not indices:
            print("⚠️ 没有匹配的有效序号！")
            custom_input("按回车继续...")
            return

        orig_backup = {idx: (self.items[idx]["checked"], self.items[idx]["custom_context"]) for idx in indices}

        for idx in indices:
            self.items[idx]["checked"] = True

        for step, idx in enumerate(indices, 1):
            ok = self.prompt_for_item_context(idx, step, len(indices))
            if not ok:
                for i, (chk, ctx) in orig_backup.items():
                    self.items[i]["checked"] = chk
                    self.items[i]["custom_context"] = ctx
                break

    def run_deselect_mode(self):
        self.render_list(edit_mode=False)
        print("【取消选中模式】请输入要取消的序号 (例如: 1, 3, 5-8 或 10~12，按 ESC 取消返回)")
        val_in = custom_input("序号: ")
        if val_in is None or not val_in:
            return

        indices = parse_range_indices(val_in, len(self.items))
        for idx in indices:
            self.items[idx]["checked"] = False
            self.items[idx]["custom_context"] = None

    def apply_and_save(self):
        generated_models: list[dict[str, Any]] = []
        for it in self.items:
            m_id: str = it["id"]
            # 明确类型为 dict[str, Any]，消除 Optional 下标警告
            entry: dict[str, Any] = copy.deepcopy(self.template_model)
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
            print("【主菜单】[1] 编辑  [2] 选中  [3] 取消选中  [4] 应用并退出 (随时按 Ctrl+C 退出)")
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
    try:
        app = CodexCatalogApp()
        app.run()
    except KeyboardInterrupt:
        print("\n\n👋 已安全退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()