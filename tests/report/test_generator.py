from predictor.data.sources import Document
from predictor.inference.forecast import ForecastResult
from predictor.report.generator import generate_report


def _doc(url):
    return Document("s", url, f"标题{url}", "内容", None, None)


def test_report_contains_conclusion_and_evidence():
    docs = [_doc("http://a"), _doc("http://b")]
    md = generate_report("油价会涨吗", 0.62, "基准率 60%", ["摘要a", "摘要b"], docs)
    assert "# 预测报告：油价会涨吗" in md
    assert "**结论概率：62%**" in md
    assert "**推理：** 基准率 60%" in md
    assert "## 依据" in md
    assert "- 摘要a  [来源](http://a)" in md
    assert "- 摘要b  [来源](http://b)"


def test_report_prior_block_only_when_given():
    docs = [_doc("http://a")]
    md = generate_report("Q", 0.5, "r", ["s"], docs, prior=0.7)
    assert "**先验参考：** 市场隐含 70%" in md
    md2 = generate_report("Q", 0.5, "r", ["s"], docs, prior=None)
    assert "先验参考" not in md2


def test_report_model_divergence_section():
    docs = [_doc("http://a")]
    runs = [
        ForecastResult(0.4, "a", "m"),
        ForecastResult(0.6, "b", "m"),
        ForecastResult(0.8, "c", "m"),
    ]
    md = generate_report("Q", 0.6, "r", ["s"], docs, runs=runs)
    assert "**模型分歧：** 40% ~ 80%（3 次采样，中位 60%）" in md


def test_report_no_divergence_without_runs():
    docs = [_doc("http://a")]
    md = generate_report("Q", 0.5, "r", ["s"], docs)
    assert "模型分歧" not in md


def test_report_disclaimer():
    md = generate_report("Q", 0.5, "r", [], [], prior=0.5)
    assert "> 本预测基于揭晓前可得公开信息；概率非事实陈述。" in md
