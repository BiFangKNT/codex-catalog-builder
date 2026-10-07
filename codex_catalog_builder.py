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
import re

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


def ensure_model_catalog_config() -> bool:
    """仅在顶层缺少目录配置时添加路径，保留原有 TOML 内容。"""
    original = CONFIG_PATH.read_bytes()
    cfg = tomllib.loads(original.decode("utf-8"))
    if "model_catalog_json" in cfg:
        return False

    # JSON 字符串的转义也适用于此处的 TOML 基本字符串。
    catalog_value = json.dumps(str(CATALOG_PATH), ensure_ascii=False).replace("\x7f", "\\u007f")
    newline = "\r\n" if b"\r\n" in original else "\n"
    prefix = f"model_catalog_json = {catalog_value}{newline}".encode("utf-8")
    temp_path = CONFIG_PATH.with_suffix(f".tmp.{os.getpid()}")
    try:
        with open(temp_path, "wb") as f:
            f.write(prefix + original)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CONFIG_PATH)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return True


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

        def read_next(timeout: float) -> bytes:
            ready, _, _ = select.select([fd], [], [], timeout)
            return os.read(fd, 1) if ready else b''

        try:
            tty.setraw(fd, when=termios.TCSANOW)
            ch = os.read(fd, 1)
            if ch == b'\x03':  # Ctrl+C
                raise KeyboardInterrupt
            if ch == b'\x1b':
                prefix = read_next(0.05)
                if not prefix:
                    return 'ESC'
                if prefix in (b'[', b'O'):
                    for _ in range(16):
                        part = read_next(0.05)
                        if not part:
                            break
                        if b'@' <= part <= b'~':
                            if part == b'A': return 'UP'
                            if part == b'B': return 'DOWN'
                            break
                return 'OTHER'
            if ch in (b'\r', b'\n'): return 'ENTER'
            if ch == b' ': return 'SPACE'
            if ch in (b'\x7f', b'\x08'): return 'BACKSPACE'

            # 直接读文件描述符，避免 TextIOWrapper 预读方向键序列的后续字节。
            first = ch[0]
            if 0xc2 <= first <= 0xdf:
                width = 2
            elif 0xe0 <= first <= 0xef:
                width = 3
            elif 0xf0 <= first <= 0xf4:
                width = 4
            else:
                width = 1
            for _ in range(width - 1):
                part = read_next(0.05)
                if not part:
                    return 'OTHER'
                ch += part
            return ch.decode('utf-8', errors='ignore')
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

def parse_model_version(slug: str) -> tuple[int, int]:
    """
    从模型 slug 中动态提取主次版本号，例如：
    'gpt-6-astra' -> (6, 0)
    'gpt-5.6-terra' -> (5, 6)
    'gpt-5.5' -> (5, 5)
    'o3-mini' -> (3, 0)
    """
    matches = re.findall(r'(?:^|[^\d])(\d+)(?:\.(\d+))?(?:[^\d]|$)', slug)
    if matches:
        major = int(matches[0][0])
        minor = int(matches[0][1]) if matches[0][1] else 0
        return (major, minor)
    return (0, 0)


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
        self.active_candidates: list[dict[str, Any]] = []
        self.template_model: dict[str, Any] = {}
        self.default_context: int = 272000

        # 1. 递进同步官方内置模型（GitHub 优先 -> Local Cache 补充）
        self.sync_builtin_models()
        # 2. 交互式选择 template 基准 (直接回车即默认)
        self.select_template_interactively()
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

    def sanitize_template(self, template: dict[str, Any]) -> None:
        """安全清洗基准模板，拔除所有特定于原模型的污染项"""
        template["upgrade"] = None
        template["availability_nux"] = None
        template["visibility"] = "list"
        template["supported_in_api"] = True
        template["auto_review_model_override"] = None
        template["comp_hash"] = None

    def get_ranked_active_models(self) -> list[dict[str, Any]]:
        """动态评估并排出所有可用的内置活跃模型"""
        candidates = []
        for idx, (slug, m) in enumerate(self.builtin_models_map.items()):
            # 1. 强力排除已停用/已配置迁移的模型
            if m.get("upgrade"):
                continue

            # 2. 必须支持 API 且有消息模板
            if not m.get("supported_in_api", True):
                continue
            if not m.get("model_messages") or not m.get("context_window"):
                continue

            # 提取排序特征
            ver = parse_model_version(slug)
            is_list = 1 if m.get("visibility") == "list" else 0
            priority = m.get("priority")
            priority_val = int(priority) if isinstance(priority, (int, float)) else 999
            ctx = int(m.get("context_window", 0))

            candidates.append({
                "model": m,
                "slug": slug,
                "version": ver,
                "is_list": is_list,
                "priority": priority_val,
                "context_window": ctx,
                "upstream_idx": idx
            })

        # 综合排序规则：
        # 1. is_list: 优先已公开发布的模型 (list 优先于 hide)
        # 2. version: 代际版本号越高越好 (6.0 > 5.6 > 5.4)
        # 3. -priority: 官方 Priority 数值越小排位越靠前 (0/1 最好)
        # 4. context_window: 窗口越大越好
        # 5. -upstream_idx: 官方列表出现越靠前越好
        candidates.sort(
            key=lambda x: (
                x["is_list"],
                x["version"],
                -x["priority"],
                x["context_window"],
                -x["upstream_idx"]
            ),
            reverse=True
        )
        return candidates

    def select_template_interactively(self):
        """启动时通过光标（↑/↓）交互式选择基准克隆模板，默认指向推荐项，回车即确认"""
        self.active_candidates = self.get_ranked_active_models()

        if not self.active_candidates:
            target = next(iter(self.builtin_models_map.values()))
            print("⚠️ 未能通过规则匹配到健康活跃模型，自动选用首个内置模型兜底。")
            self.template_model = copy.deepcopy(target)
            self.sanitize_template(self.template_model)
            self.default_context = int(self.template_model.get("context_window", 272000))
            return

        # 取前 8 个候选模型展示
        display_count = min(len(self.active_candidates), 8)
        candidates_to_show = self.active_candidates[:display_count]
        cursor_idx = 0  # 初始光标停在第 1 项（最推荐的主力旗舰）

        while True:
            # 清屏重绘光标选择界面
            sys.stdout.write("\033[H\033[J")
            print("🎯 请选择基准克隆模板（官方内置活跃旗舰模型）：")
            print("💡 操作：[↑/↓] 移动光标 | [Enter] 确认选择（默认已高亮推荐项，可直接回车）")
            print("=" * 86)

            for i, c in enumerate(candidates_to_show):
                vis_tag = "公开发布 [list]" if c["is_list"] else "隐藏预览 [hide]"
                ver_str = f"v{c['version'][0]}.{c['version'][1]}"
                rec_tag = " [★ 官方首推]" if i == 0 else ""
                
                line = f"{i + 1}. {c['slug']:<24} {ver_str:<6} | {vis_tag:<16} | Priority: {c['priority']:<2} | {c['context_window']:,} tokens{rec_tag}"

                # 选中行反色高亮显示
                if i == cursor_idx:
                    sys.stdout.write(f"\033[1;36;7m > {line:<82}\033[0m\n")
                else:
                    sys.stdout.write(f"   {line}\n")

            print("=" * 86)

            k = get_key()
            if k == 'UP':
                cursor_idx = (cursor_idx - 1) % len(candidates_to_show)
            elif k == 'DOWN':
                cursor_idx = (cursor_idx + 1) % len(candidates_to_show)
            elif k in ('ENTER', 'SPACE'):
                break
            elif k == 'ESC':
                cursor_idx = 0  # 按 ESC 默认恢复第一项退出
                break

        target = candidates_to_show[cursor_idx]["model"]

        # 克隆并安全清洗模板
        self.template_model = copy.deepcopy(target)
        self.sanitize_template(self.template_model)
        self.default_context = int(self.template_model.get("context_window", 272000))

        # 选完后简单提示，随即开始拉取代理模型
        print(f"\n✅ 已选定基准克隆模板: \033[1;36m{self.template_model.get('slug')}\033[0m (Context: {self.default_context:,})")

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
        max_display = max(6, term_height - 12)
        
        scroll_top = max(0, min(self.cursor_idx - max_display // 2, len(self.items) - max_display))
        scroll_bottom = min(len(self.items), scroll_top + max_display)

        sys.stdout.write("\033[H\033[J")
        
        tmpl_slug = self.template_model.get("slug", "未知")
        tmpl_ver = parse_model_version(tmpl_slug)
        ver_str = f"v{tmpl_ver[0]}.{tmpl_ver[1]}"
        
        print(f"📦 Codex Catalog 配置器 | Provider: \033[1;32m{self.provider_name}\033[0m | 内置模型库: {len(self.builtin_models_map)} 个")
        print(f"🎯 当前基准模板: \033[1;36m{tmpl_slug}\033[0m ({ver_str} | Priority: {self.template_model.get('priority', 0)} | Context: {self.default_context:,})")
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
                    if cur["custom_context"] is None:
                        cur["custom_context"] = self.default_context
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
                    elif it["custom_context"] is None:
                        it["custom_context"] = self.default_context
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
            # 基于清洗后的模板深拷贝
            entry: dict[str, Any] = copy.deepcopy(self.template_model)
            entry["slug"] = m_id
            entry["display_name"] = m_id
            entry["visibility"] = "list"
            entry["supported_in_api"] = True
            
            # 【双保险】清除退役标记与特异性字段
            entry["upgrade"] = None
            entry["availability_nux"] = None
            entry["priority"] = start_custom_priority + custom_added_count

            # 同步更新 context_window 与 max_context_window
            ctx_val = it["custom_context"] if (it["state"] == "custom" and it["custom_context"] is not None) else self.default_context
            entry["context_window"] = ctx_val
            entry["max_context_window"] = ctx_val

            final_catalog.append(entry)
            custom_added_count += 1

        # 4. 原子安全写入
        atomic_save_json(CATALOG_PATH, {"models": final_catalog})

        # 5. 目录保存成功后补充顶层配置，已有配置保持不变。
        try:
            config_added = ensure_model_catalog_config()
        except Exception as e:
            print(f"\n⚠️ 目录已保存至 {CATALOG_PATH}，但更新 {CONFIG_PATH} 失败: {e}")
            sys.exit(1)

        print("\n" + "=" * 80)
        print(f"🎉 成功生成 Model Catalog！总计包含 {len(final_catalog)} 个模型：")
        print(f"   ├─ 🏛️ 官方内置模型: {builtin_count} 个 (最大 Priority: {max_builtin_priority})")
        print(f"   ├─ 🚀 自定义模型:   {custom_added_count} 个 (Priority 范围: {start_custom_priority} ~ {start_custom_priority + custom_added_count - 1})")
        print(f"   └─ 🚫 已排除模型:   {excluded_count} 个")
        print(f"📁 已安全原子写入至: {CATALOG_PATH}")
        if config_added:
            print(f"📝 已在 {CONFIG_PATH} 顶层添加 model_catalog_json。")
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
