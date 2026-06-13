---
name: code-gen-ppt
description: 当用户要求生成PPT时，调用 codex-ppt 与 imagegen 生成简体中文演示文稿，支持默认页数与默认风格策略。
---

# code-gen-ppt

## 何时使用

当用户提出以下意图时使用本技能：

- 生成 PPT / PTT / 演示文稿
- 把某个文档（如 PDF）转成 PPT
- 明确要求套用某种 PPT 风格

## 风格枚举

支持的风格仅限以下值：

- 清爽专业风
- 创意杂志风
- 电子墨水杂志风
- 数据仪表盘风
- 复古扁平插画风
- 手绘技术解释风
- 手绘白板风
- 温暖手工风
- 科研答辩风
- 麦肯锡风格

默认规则：

- 当用户未指定风格时，使用 `清爽专业风`。

## 页数规则

- 当用户未指定 PPT 页数时，使用 `10` 页。

## 执行指令规则

### 场景1：用户直接要求生成 PPT（未提供参考文件）

原始指令如下：

```bash
codex exec "使用 codex-ppt skill年美伊战争做成简体中文的 PPT, 电子墨水杂志风 ,图片生成请使用codex 内建的imagegen skill " --skip-git-repo-check --sandbox workspace-write
```

执行时使用如下命令模板（沿用原始指令语义并补充页数控制）：

```bash
codex exec "使用 codex-ppt skill 把 {主题}做成{页数}页简体中文的 PPT, {风格},图片生成请使用codex 内建的imagegen skill " --skip-git-repo-check --sandbox workspace-write
```

参数替换规则：

- `{主题}`：优先使用用户主题；未指定时使用 `美伊战争`。
- `{页数}`：优先使用用户指定页数；未指定时使用 `10`。
- `{风格}`：优先使用用户指定风格；未指定时使用 `清爽专业风`。

### 场景2：用户先上传参考文件，再要求生成 PPT

执行如下命令模板：

```bash
codex exec "使用 codex-ppt skill 把 @doc\\xxxxx.pdf 做成{页数}页简体中文的 PPT, {风格},图片生成请使用codex 内建的imagegen skill " --skip-git-repo-check --sandbox workspace-write
```

参数替换规则：

- `@doc\\xxxxx.pdf`：替换为用户实际上传文件路径（位于 `doc/` 目录）。
- `{页数}`：优先使用用户指定页数；未指定时使用 `10`。
- `{风格}`：优先使用用户指定风格；未指定时使用 `清爽专业风`。

## 约束

- 生成语言固定为简体中文。
- 图片生成固定要求使用 codex 内建 `imagegen` skill。
- 调用 `codex exec` 时保留参数：`--skip-git-repo-check --sandbox workspace-write`。
