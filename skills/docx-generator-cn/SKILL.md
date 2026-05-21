mmbc---
name: DOCX Generator
slug: docx-generator
version: 1.0.0
homepage: https://clawic.com/skills/docx-generator
description: 生成 Word DOCX 文件的实用指南，适用于中文报告、通知、合同、模板化文档等场景。
metadata:
{
"clawdbot":
{
"emoji": "📝",
"requires": { "bins": [] },
"os": ["linux", "darwin", "win32"],
},
}

---

## 何时使用

当用户需要通过代码创建 Word 文档（.docx）时使用本技能。典型场景包括：自动生成中文报告、通知单、合同初稿、批量模板文档。

## 作用范围

本技能只做以下事情：

- 提供生成 DOCX 的代码模式与实现建议
- 说明标题、段落、列表、表格等常见结构写法
- 给出中文文档生成和校验的实践建议

本技能不会：

- 执行代码或直接生成文件
- 发起网络请求
- 访问用户工作目录之外的文件

所有示例代码仅供用户在本地实现。

## 核心示例（直接生成 DOCX）

```python
from docx import Document

doc = Document()

doc.add_heading("中文测试文档", level=1)
doc.add_paragraph("这是一个通过 python-docx 直接生成的段落。")
doc.add_paragraph("第二段：可用于报告、通知、合同等正文内容。")

table = doc.add_table(rows=1, cols=2)
hdr = table.rows[0].cells
hdr[0].text = "项目"
hdr[1].text = "说明"

row = table.add_row().cells
row[0].text = "状态"
row[1].text = "已生成"

doc.save("test.docx")
print("已生成 test.docx")
```

## 备选示例（HTML 转 DOCX）

```python
from html2docx import html2docx

html = """
<p style="font-family: Microsoft YaHei; font-size:14px;">
这是中文段落，显示应该正常。
</p>
"""

doc = html2docx(html, title="中文测试")

with open("test.docx", "wb") as f:
    f.write(doc.getvalue())

print("已生成 test.docx")
```

## 核心规则

### 1. 默认优先 python-docx

需要精确控制标题、段落、表格、分页时，优先使用 `python-docx` 直接构建文档。

### 2. HTML 输入再考虑 html2docx

当上游已产出 HTML（如模板引擎输出）时，再使用 `html2docx` 转换，避免重复开发。

### 3. 中文内容先做最小验证

先生成一个最小中文样例（标题 + 段落），确认字体和换行正常，再扩展到完整模板。

### 4. 文件输出方式要正确

- `python-docx` 使用 `doc.save("xx.docx")`
- `html2docx` 使用二进制写入 `doc.getvalue()`

## 常见问题

| 问题         | 现象                | 处理方式                           |
| ------------ | ------------------- | ---------------------------------- |
| 文档打不开   | 文件损坏或空文件    | 检查是否使用了正确保存方式         |
| 中文显示异常 | 字体或样式不一致    | 先用最小中文样例验证，再逐步加样式 |
| 布局不可控   | 段落/表格效果不稳定 | 改用 `python-docx` 直接构建结构    |

## 输出校验

生成后建议检查：

1. 文件大小大于 0
2. Word 可正常打开
3. 中文标题和正文显示正常
4. 表格与段落结构符合预期

## 安全与隐私

- 文档生成在本地完成
- 不要求上传内容到外部服务
- 避免在示例中硬编码敏感信息

## 反馈

- 如果有帮助：`clawhub star docx-generator`
- 获取更新：`clawhub sync`
