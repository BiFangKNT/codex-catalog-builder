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

# ==================== 1. 路径与远端配置 ====================
CONFIG_PATH = Path.home() / ".codex" / "config.toml"
CACHE_PATH = Path.home() / ".codex" / "models_cache.json"
CATALOG_PATH = Path.home() / ".codex" / "custom_catalog.json"

GITHUB_MODELS_URL = "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/models.json"


# ==================== 2. 原子写入与配置读取 ====================
def atomic_save_json(path: Path, data: dict[str, Any]) -> None:
    """原子写入 JSON 文件，防止断电/强退导致文件损坏"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # 强制刷入物理磁盘
        os.replace(temp_path, path)  # 原子替换
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise


def load_proxy_config() -> tuple[str, str, str]:
    """从 ~/.codex/config.toml 中读取 model_provider 配置"""
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
        if ch == b'\x03':  # Ctrl+C
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
            if ch == '\x03':  # Ctrl+C
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
    """支持 ESC 取消、Ctrl+C 强退、退格的交互式输入"""
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
        self.builtin_models_map: dict[str, dict[str, Any]] = {}
        self.template_model: dict[str, Any] = {}
        self.default_context: int = 272000

        # 1. 递进同步官方内置模型（GitHub 优先 -> Local Cache 补充）
        self.sync_builtin_models()
        # 2. 从内置模型中提取 template 基准
        self.extract_template_from_builtins()
        # 3. 从代理拉取自定义模型并过滤/回显
        self.fetch_and_init_models()

    def sync_builtin_models(self):
        """
        二级同步策略：
        第一级：在线同步 GitHub 官方源（openai/codex/main/.../models.json）
        第二级：本地 Cache（~/.codex/models_cache.json）补充第一级中没有的 slug
        """
        print("🔄 正在同步官方内置模型定义 ...")
        online_count = 0
        local_count = 0

        # 1. 第一级：GitHub 在线源
        try:
            resp = requests.get(GITHUB_MODELS_URL, timeout=4)
            if resp.status_code == 200:
                raw_data = resp.json()
                models_list = raw_data.get("models", []) if isinstance(raw_data, dict) else (raw_data if isinstance(raw_data, list) else [])
                for m in models_list:
                    if isinstance(m, dict) and "slug" in m:
                        slug = str(m["slug"])
                        self.builtin_models_map[slug] = m
                        online_count += 1
        except Exception as e:
            print(f"⚠️ 在线同步 GitHub 源超时/失败 ({e})，将回退至本地缓存。")

        # 2. 第二级：本地 Cache 兜底
        if CACHE_PATH.exists():
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    raw_models = data.get("models", []) if isinstance(data, dict) else (list(data.values()) if isinstance(data, dict) else [])
                    for m in raw_models:
                        if isinstance(m, dict) and "slug" in m:
                            slug = str(m["slug"])
                            # 过滤第一级已有的 slug，仅补充缺失项
                            if slug not in self.builtin_models_map:
                                self.builtin_models_map[slug] = m
                                local_count += 1
            except Exception as e:
                print(f"⚠️ 读取本地 models_cache 失败: {e}")

        if not self.builtin_models_map:
            raise RuntimeError(
                "❌ 无法获取任何内置模型定义！\n"
                "原因：在线 GitHub 同步失败且本地 ~/.codex/models_cache.json 不存在。\n"
                "解决办法：请检查网络连接，或先启动一次官方 Codex 客户端生成本地缓存。"
            )

        print(f"✅ 内置模型同步完成 (GitHub 在线: {online_count} 个, 本地 Cache 补充: {local_count} 个, 共 {len(self.builtin_models_map)} 个)")

    def extract_template_from_builtins(self):
        """动态提取基准模板"""
        target = self.builtin_models_map.get("gpt-5.4")
        if not target:
            # 优先寻找任意 gpt-5 系模型
            for slug, m in self.builtin_models_map.items():
                if "gpt-5" in slug:
                    target = m
                    break
        if not target:
            # 兜底选取第一个内置模型
            target = next(iter(self.builtin_models_map.values()))

        self.template_model = copy.deepcopy(target)
        self.default_context = int(self.template_model.get("context_window", 272000))

    def load_existing_catalog(self) -> dict[str, dict[str, Any]] | None:
        """读取已有的 custom_catalog.json 以实现状态回显与幂等"""
        if not CATALOG_PATH.exists():
            return None
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                models = data.get("models", [])
                return {m["slug"]: m for m in models if isinstance(m, dict) and "slug" in m}
        except Exception:
            return None

    def fetch_and_init_models(self):
        """从代理 API 获取模型列表并过滤内置模型"""
        url = f"{self.proxy_base.rstrip('/')}/models"
        headers: dict[str, str] = {}
        if self.proxy_key:
            headers["Authorization"] = f"Bearer {self.proxy_key}"

        print(f"正在从 [{self.provider_name}] 获取代理模型列表 ...")
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as e:
            print(f"❌ 获取模型失败: {e}")
            sys.exit(1)

        existing_catalog = self.load_existing_catalog()
        filtered_builtin_count = 0

        for item in data:
            m_id = str(item.get("id", ""))
            if not m_id:
                continue

            # 1. 过滤已在官方内置库中的模型（避免在自定义列表中重复展示）
            if m_id in self.builtin_models_map:
                filtered_builtin_count += 1
                continue

            # 2. 自定义模型的状态回显
            state = "included"  # 'included' ([ ]), 'custom' ([x]), 'excluded' ([-])
            custom_ctx: int | None = None

            if existing_catalog is not None:
                if m_id in existing_catalog:
                    entry = existing_catalog[m_id]
                    ctx = entry.get("context_window")
                    if ctx is not None and ctx != self.default_context:
                        state = "custom"
                        custom_ctx = int(ctx)
                    else:
                        state = "included"
                else:
                    state = "excluded"

            self.items.append({
                "id": m_id,
                "state": state,
                "custom_context": custom_ctx
            })

        if filtered_builtin_count > 0:
            print(f"ℹ️ 已过滤 {filtered_builtin_count} 个代理中与官方内置同名的模型（官方定义已自动保留）。")

        if not self.items:
            print("⚠️ 代理未返回任何非内置模型。")

    def render_list(self, edit_mode: bool = False):
        term_height = shutil.get_terminal_size().lines
        max_display = max(8, term_height - 11)
        
        scroll_top = max(0, min(self.cursor_idx - max_display // 2, len(self.items) - max_display))
        scroll_bottom = min(len(self.items), scroll_top + max_display)

        sys.stdout.write("\033[H\033[J")
        print(f"📦 Codex Catalog 配置器 | Provider: \033[1;32m{self.provider_name}\033[0m | 内置模型: {len(self.builtin_models_map)} 个 | 基准 Context: {self.default_context:,}")
        print("=" * 80)

        for idx in range(scroll_top, scroll_bottom):
            item = self.items[idx]
            st = item["state"]

            if st == "custom":
                check_mark = "\033[1;32m[x]\033[0m"
                val_str = f"{item['custom_context']:,} tokens"
            elif st == "excluded":
                check_mark = "\033[1;31m[-]\033[0m"
                val_str = "\033[90m(已排除 - 不写入)\033[0m"
            else:  # included
                check_mark = "[ ]"
                val_str = f"(默认: {self.default_context:,})"

            line = f"{check_mark} {idx + 1:2d}. {item['id']:<42} Context: {val_str}"

            if edit_mode and idx == self.cursor_idx:
                sys.stdout.write(f"\033[1;36;7m> {line:<76}\033[0m\n")
            else:
                sys.stdout.write(f"  {line}\n")

        print("=" * 80)

    def prompt_for_item_context(self, idx: int, current_step: int, total_steps: int) -> bool:
        item = self.items[idx]
        cur_val = item["custom_context"] or self.default_context
        prompt = (
            f"\n配置 [{current_step}/{total_steps}] 模型 \033[1;33m{item['id']}\033[0m 的 context_window\n"
            f"(当前: {cur_val:,}，支持输入如 200k/128k/1m，直接回车保持，按 ESC 放弃本次操作): "
        )
        val_in = custom_input(prompt)
        if val_in is None:
            return False
        if val_in:
            item["custom_context"] = parse_token_input(val_in, cur_val)
        elif item["custom_context"] is None:
            item["custom_context"] = cur_val
        item["state"] = "custom"
        return True

    def run_edit_mode(self):
        orig_states = [(it["state"], it["custom_context"]) for it in self.items]
        
        while True:
            self.render_list(edit_mode=True)
            print("【编辑模式】[↑/↓]移动 | [空格]自定义[x] | [e]排除/恢复[-] | [a]全选[x] | [x]全排除[-] | [Enter]确定 | [ESC]取消")
            
            key = get_key()
            if key == 'UP':
                self.cursor_idx = (self.cursor_idx - 1) % len(self.items)
            elif key == 'DOWN':
                self.cursor_idx = (self.cursor_idx + 1) % len(self.items)
            elif key == 'SPACE':
                cur = self.items[self.cursor_idx]
                if cur["state"] == "custom":
                    cur["state"] = "included"
                    cur["custom_context"] = None
                else:
                    cur["state"] = "custom"
            elif key in ('e', 'E'):
                cur = self.items[self.cursor_idx]
                if cur["state"] == "excluded":
                    cur["state"] = "included"
                else:
                    cur["state"] = "excluded"
                    cur["custom_context"] = None
            elif key in ('a', 'A'):
                all_custom = all(it["state"] == "custom" for it in self.items)
                for it in self.items:
                    it["state"] = "included" if all_custom else "custom"
                    if it["state"] == "included":
                        it["custom_context"] = None
            elif key in ('x', 'X'):
                all_excluded = all(it["state"] == "excluded" for it in self.items)
                for it in self.items:
                    it["state"] = "included" if all_excluded else "excluded"
                    if it["state"] == "excluded":
                        it["custom_context"] = None
            elif key == 'ESC':
                for i, (st, ctx) in enumerate(orig_states):
                    self.items[i]["state"] = st
                    self.items[i]["custom_context"] = ctx
                break
            elif key == 'ENTER':
                custom_indices = [i for i, it in enumerate(self.items) if it["state"] == "custom"]
                if custom_indices:
                    canceled = False
                    for step, idx in enumerate(custom_indices, 1):
                        ok = self.prompt_for_item_context(idx, step, len(custom_indices))
                        if not ok:
                            for i, (st, ctx) in enumerate(orig_states):
                                self.items[i]["state"] = st
                                self.items[i]["custom_context"] = ctx
                            canceled = True
                            break
                    if canceled:
                        break
                break

    def run_select_mode(self):
        self.render_list(edit_mode=False)
        print("【选中模式】请输入要自定义 Context 的序号 (例如: 1, 3, 5-8 或 10~12，按 ESC 返回)")
        val_in = custom_input("序号: ")
        if val_in is None or not val_in:
            return

        indices = parse_range_indices(val_in, len(self.items))
        if not indices:
            print("⚠️ 没有匹配的有效序号！")
            custom_input("按回车继续...")
            return

        orig_backup = {idx: (self.items[idx]["state"], self.items[idx]["custom_context"]) for idx in indices}

        for idx in indices:
            self.items[idx]["state"] = "custom"

        for step, idx in enumerate(indices, 1):
            ok = self.prompt_for_item_context(idx, step, len(indices))
            if not ok:
                for i, (st, ctx) in orig_backup.items():
                    self.items[i]["state"] = st
                    self.items[i]["custom_context"] = ctx
                break

    def run_deselect_mode(self):
        self.render_list(edit_mode=False)
        print("【取消选中模式】请输入要重置为默认的序号 (例如: 1, 3, 5-8，按 ESC 返回)")
        val_in = custom_input("序号: ")
        if val_in is None or not val_in:
            return

        indices = parse_range_indices(val_in, len(self.items))
        for idx in indices:
            self.items[idx]["state"] = "included"
            self.items[idx]["custom_context"] = None

    def run_exclude_mode(self):
        self.render_list(edit_mode=False)
        print("【排除模式】请输入要排除（不写入文件）的序号 (例如: 1, 3, 5-8，按 ESC 返回)")
        val_in = custom_input("序号: ")
        if val_in is None or not val_in:
            return

        indices = parse_range_indices(val_in, len(self.items))
        for idx in indices:
            self.items[idx]["state"] = "excluded"
            self.items[idx]["custom_context"] = None

    def run_unexclude_mode(self):
        self.render_list(edit_mode=False)
        print("【取消排除模式】请输入要恢复写入的序号 (例如: 1, 3, 5-8，按 ESC 返回)")
        val_in = custom_input("序号: ")
        if val_in is None or not val_in:
            return

        indices = parse_range_indices(val_in, len(self.items))
        for idx in indices:
            self.items[idx]["state"] = "included"
            self.items[idx]["custom_context"] = None

    def apply_and_save(self):
        # 1. 首先注入全量官方内置模型（保留原生配置）
        final_catalog: list[dict[str, Any]] = [
            copy.deepcopy(m) for m in self.builtin_models_map.values()
        ]
        builtin_count = len(final_catalog)

        # 2. 动态计算内置模型中的最大 priority
        builtin_priorities = [
            int(m.get("priority", 0)) for m in self.builtin_models_map.values()
            if isinstance(m.get("priority"), (int, float))
        ]
        max_builtin_priority = max(builtin_priorities, default=0)
        start_custom_priority = max_builtin_priority + 1

        # 3. 追加用户自定义模型
        custom_added_count = 0
        excluded_count = 0

        for it in self.items:
            if it["state"] == "excluded":
                excluded_count += 1
                continue

            m_id: str = it["id"]
            entry: dict[str, Any] = copy.deepcopy(self.template_model)
            entry["slug"] = m_id
            entry["display_name"] = m_id
            entry["visibility"] = "list"
            entry["supported_in_api"] = True
            
            # 自定义模型优先级从 max_builtin_priority + 1 开始顺延
            entry["priority"] = start_custom_priority + custom_added_count

            if it["state"] == "custom" and it["custom_context"] is not None:
                entry["context_window"] = it["custom_context"]
            else:
                entry["context_window"] = self.default_context

            final_catalog.append(entry)
            custom_added_count += 1

        # 4. 原子安全写入
        atomic_save_json(CATALOG_PATH, {"models": final_catalog})

        print("\n" + "=" * 80)
        print(f"🎉 成功生成 Model Catalog！总计包含 {len(final_catalog)} 个模型：")
        print(f"   ├─ 🏛️ 官方内置模型: {builtin_count} 个 (最大 Priority: {max_builtin_priority})")
        print(f"   ├─ 🚀 自定义模型:   {custom_added_count} 个 (Priority 范围: {start_custom_priority} ~ {start_custom_priority + custom_added_count - 1})")
        print(f"   └─ 🚫 已排除模型:   {excluded_count} 个")
        print(f"📁 已安全原子写入至: {CATALOG_PATH}")
        print("=" * 80)
        sys.exit(0)

    def run(self):
        while True:
            self.render_list(edit_mode=False)
            print("【主菜单】[1] 编辑  [2] 选中  [3] 取消选中  [4] 排除  [5] 恢复排除  [6] 应用并退出")
            sys.stdout.write("请按数字键选择 (1-6, Ctrl+C 退出): ")
            sys.stdout.flush()

            key = get_key()
            if key == '1':
                self.run_edit_mode()
            elif key == '2':
                self.run_select_mode()
            elif key == '3':
                self.run_deselect_mode()
            elif key == '4':
                self.run_exclude_mode()
            elif key == '5':
                self.run_unexclude_mode()
            elif key == '6':
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