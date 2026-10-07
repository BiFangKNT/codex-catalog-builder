# Repository Guidelines

## 项目结构与模块组织

- `codex_catalog_builder.py` 是唯一的运行时模块，包含配置读取、跨平台按键处理、模型同步、模板选择、TUI 和原子保存逻辑；按现有功能分区组织修改。
- `pyproject.toml` 声明 Python ≥ 3.13、`requests` 依赖和 Hatchling 构建配置；`cxc` 与 `codex-catalog` 均指向 `main()`。`uv.lock` 记录依赖锁定结果。
- `README.md` 提供中文使用说明；`codex-catalog.spec` 配置 PyInstaller；`.github/workflows/publish.yml` 负责标签发布。`build/`、`dist/` 是生成目录，不应提交。
- 当前没有独立的测试目录或静态资源目录。

## 构建与开发命令

在仓库根目录执行：

- `uv sync --locked`：按锁文件同步开发环境。
- `uv run codex_catalog_builder.py`：启动交互式程序；也可使用 `uv run cxc` 或 `uv run codex-catalog`。
- `uv build`：生成 wheel 和源码分发包，输出到 `dist/`。
- `uv tool run twine check --strict dist/*`：检查分发包元数据，与发布工作流一致。
- `uv run --with pyinstaller pyinstaller codex-catalog.spec`：按现有配置生成控制台可执行文件。

## 编码风格与命名

使用四空格缩进，函数和变量采用 `snake_case`，模块常量采用 `UPPER_SNAKE_CASE`。沿用现有类型注解，如 `Path`、`dict[str, Any]` 和 `str | None`，以及中文注释与用户提示。仓库尚未配置统一格式化或 lint 工具；避免混入无关的全文件格式调整。文件读写明确使用 UTF-8，保留原子保存和终端状态恢复行为。

## 测试与验证

目前没有自动化测试框架或覆盖率门槛。修改后记录实际验证步骤与结果，重点检查模板选择、模型去重、Context 输入、排除与恢复、ESC 撤销，以及 JSON 保存。按键处理变更需分别验证 Windows 与 POSIX 终端；未验证的平台应在 PR 中说明。新增自动化测试可采用标准库 `unittest`，命名为 `tests/test_*.py`，通过 `uv run python -m unittest discover -s tests` 执行；使用临时目录和模拟网络响应隔离真实配置。

## 提交与 Pull Request

近期历史采用 `feat(scope): …`、`fix(scope): …`、`docs(readme): …`、`build(packaging): …` 和 `release: …`。提交标题说明具体行为变化。PR 应描述问题、变更和验证结果，关联相关 issue；TUI 展示变化附截图或终端记录。发布标签须为 `v<版本号>`，并与 `pyproject.toml` 一致；推送该标签会触发 PyPI 发布。

## 配置安全

程序读取 `~/.codex/config.toml` 和 `models_cache.json`，写入 `custom_catalog.json`。验证使用隔离配置，不提交认证 Token、私人代理地址或真实目录内容。
