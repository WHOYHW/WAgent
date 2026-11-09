"""切块三策略单测（文档 18 §三 v1.2，AC-18a，零 IO 零网络）。

覆盖：
1. by_clause：条款锚识别（中文数字进位含"第一百零一条"）、前言独立块、
   无条款回退滑窗、超长条款滑窗共享锚且相邻片重叠；
2. by_heading / by_qa：锚定节、无锚回退；
3. 通用不变量：块长 ≤ max_chars、section_idx 自 0 连续、不丢正文
   （原文每个非空行可在块并集中找到）、参数不变量拒劣配置；
4. AC-18a：三份真实 fixture（校规条款页/办事流程页/FAQ）全部 ≤500 字不丢正文；
5. config 对齐：chunking 默认值 == Settings.kb_chunk_max_chars。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.kb.chunking import (
    CHUNK_DEFAULT_MAX_CHARS,
    CHUNK_DEFAULT_OVERLAP,
    KbChunkDraft,
    by_clause,
    by_heading,
    by_qa,
)

MAXC = CHUNK_DEFAULT_MAX_CHARS  # 500
OVERLAP = CHUNK_DEFAULT_OVERLAP  # 50


# ---- 通用断言 ----

def assert_no_loss(text: str, drafts: list[KbChunkDraft], *, max_chars: int = MAXC) -> None:
    """不变量三连：非空产出、块长钳制、不丢正文（每个非空行可在某块中找到）。"""
    assert drafts, "空输入以外必须产出非空块列表"
    assert all(len(d.text) <= max_chars for d in drafts)
    assert [d.section_idx for d in drafts] == list(range(len(drafts)))
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for line in lines:
        assert any(line in d.text for d in drafts), f"正文行丢失：{line[:40]}"


def _long_line(chars: int) -> str:
    return ("转专业申请应当在本学期的规定窗口内提交材料。" * 3)[:chars]


# ---- AC-18a 三份真实 fixture ----

FIXTURE_REGULATION = """\
《普通高等学校学生管理规定》实施细则（试行）

第一章 总则

第一条 为规范学生管理，依据教育部令第 41 号及学校有关规定，制定本细则。
第二条 本细则适用于在校全日制本科学生的学籍管理与日常管理。

第二章 学分与成绩

第十二条 学生在校期间依法享有下列权利，学校应当予以保障：
    （一）参加教育教学计划安排的各项活动，使用教育教学设施设备；
    （二）按学校规定选择专业与课程，完成学业后获得相应的学业证书；
    （三）对学校给予的处理处分有异议，向学校学生申诉处理委员会提出申诉；
    （四）法律法规规定的其他权利。
第十三条 课程考核分为考试与考查两种形式，考核成绩计入成绩档案。
    重修课程的成绩按重修实际成绩记载，并标注重修字样，原成绩保留在档案中备查，
    不因重修覆盖原记录，这是成绩管理的底线要求，任何单位和个人不得更改。
第一百零一条 本细则自 2026 年 9 月 1 日起施行，由教务处负责解释。原有规定与本细则不一致的，以本细则为准。
"""

FIXTURE_GUIDE = """\
# 转专业办理指南

转专业每学年集中办理一次，具体窗口以教务处当学年通知为准。

## 申请条件

- 平均学分绩点排名在本专业前 20%；
- 无未解除的违纪处分；
- 已修课程无不及格记录。

## 办理流程

1. 学期末在教务系统"学籍异动"栏目提交转专业申请，上传个人陈述；
2. 教务处进行资格审查，审查结果在教务处主页公示三个工作日；
3. 通过初审后参加转入学院的考核，考核形式为面试或笔试，具体由学院通知；
4. 考核通过的学生名单报学校教学委员会审定，审定通过后办理学籍异动手续，
   学籍信息更新后学生应在新专业所在学院报到注册，原专业课程学分按学校
   学分认定办法进行认定与转换，认定结果以教务系统记录为准，学生如对认定
   结果有异议可在公示期内向教务处提出复核申请，复核期间不影响异动办理。
## 材料清单

- 转专业申请表（教务系统下载）；
- 个人陈述一份（不少于 800 字）；
- 成绩单（教务系统自助打印）。
"""

FIXTURE_FAQ = """\
常见问题汇总（教务处整理）

问：图书馆开放到几点？
答：周一至周五 8:00-22:00，周末 9:00-21:00，考试周延长至 23:00。

问：补考什么时候报名？
答：开学第一周内在教务系统报名，逾期不受理。

问：怎么办理走读？
答：走读申请每学期受理一次。学生需提交家长签字的走读申请书、住宿费缴费
    凭证复印件以及本人身份证复印件，交至学生工作处住宿管理科。住宿管理科
    在收到材料后十个工作日内完成审核，审核结果通过辅导员通知本人。走读
    期间不再享有宿舍床位，水电费按实际住宿月份结算，退还标准参照后勤
    保障处的收费公示执行，如有疑问可到后勤服务大厅咨询或拨打服务热线。
问：学生证丢失怎么补办？
答：在教务系统"证明打印"栏目申请补办，三个工作日后到教务处窗口领取。
"""


# ---- by_clause：校规主策略 ----


def test_by_clause_anchors_in_order_with_preamble():
    drafts = by_clause(FIXTURE_REGULATION)
    anchors = [d.anchor for d in drafts]
    assert anchors[0] is None  # 前言（标题 + 第一章）独立成块
    assert "第一条" in anchors and "第一百零一条" in anchors  # 中文数字进位识别
    assert anchors.index("第一条") < anchors.index("第十二条") < anchors.index("第一百零一条")


def test_by_clause_chinese_numeral_progression():
    text = "第十条 甲。第九十九条 乙。第一百零一条 丙。第一百二十条 丁。"
    anchors = [d.anchor for d in by_clause(text)]
    assert anchors == ["第十条", "第九十九条", "第一百零一条", "第一百二十条"]


def test_by_clause_fallback_when_no_anchor():
    text = "\n".join(f"通知正文第{i}行，内容为常规长度。" for i in range(40))
    drafts = by_clause(text)
    assert all(d.anchor is None for d in drafts)
    assert len(drafts) > 1  # 600+ 字超限 → 滑窗多片
    assert_no_loss(text, drafts)


def test_by_clause_oversized_clause_slides_with_shared_anchor():
    # 单条款 8 行 × ~105 字 ≈ 840 字 → 滑窗多片共享锚，逐行不丢
    body = "\n".join("本条详细说明转专业资格审核的具体操作要求。" * 5 for _ in range(8))
    text = f"第一条 总则性说明。\n第十二条 {body}"
    drafts = by_clause(text)
    oversized = [d for d in drafts if d.anchor == "第十二条"]
    assert len(oversized) > 1
    assert all(len(d.text) <= MAXC for d in oversized)
    # 相邻滑窗片重叠：后片头部 = 前片尾部 overlap 字符
    assert oversized[1].text.startswith(oversized[0].text[-OVERLAP:])
    assert_no_loss(text, drafts)


# ---- by_heading：办事指南 ----


def test_by_heading_anchors_and_structure():
    drafts = by_heading(FIXTURE_GUIDE)
    anchors = [d.anchor for d in drafts]
    assert anchors[0] == "转专业办理指南"  # 首行即 H1，无导语块
    assert "办理流程" in anchors and "材料清单" in anchors


def test_by_heading_fallback_when_no_heading():
    text = "\n".join(f"指南正文第{i}行，无 markdown 标题。" for i in range(40))
    drafts = by_heading(text)
    assert all(d.anchor is None for d in drafts)
    assert_no_loss(text, drafts)


# ---- by_qa：FAQ ----


def test_by_qa_pairs_with_question_anchor():
    drafts = by_qa(FIXTURE_FAQ)
    anchors = [d.anchor for d in drafts]
    assert anchors[0] is None  # 汇总标题导语
    assert any(a and a.startswith("问：图书馆开放到几点") for a in anchors)
    # 问题与答案同块（FTS 双向可查）
    qd = next(d for d in drafts if d.anchor and d.anchor.startswith("问：补考"))
    assert "开学第一周内在教务系统报名" in qd.text


def test_by_qa_fallback_when_no_question():
    text = "\n".join(f"普通页面第{i}行，无问答对。" for i in range(40))
    drafts = by_qa(text)
    assert all(d.anchor is None for d in drafts)
    assert_no_loss(text, drafts)


# ---- 通用不变量 ----


@pytest.mark.parametrize("chunker", [by_clause, by_heading, by_qa])
def test_empty_input_returns_empty(chunker):
    assert chunker("") == []
    assert chunker("  \n \n ") == []


def test_param_invariant_rejects_bad_config():
    with pytest.raises(ValueError, match="不变量"):
        by_clause("任意", max_chars=50, overlap=50)  # overlap >= max_chars


def test_chunk_draft_contract_rejects_empty_text():
    with pytest.raises(ValidationError):
        KbChunkDraft(section_idx=0, text="")


# ---- AC-18a：三份 fixture 全量收敛 ----


@pytest.mark.parametrize(
    ("fixture", "chunker"),
    [
        (FIXTURE_REGULATION, by_clause),
        (FIXTURE_GUIDE, by_heading),
        (FIXTURE_FAQ, by_qa),
    ],
    ids=["regulation", "guide", "faq"],
)
def test_ac_18a_all_chunks_within_budget_and_no_loss(fixture, chunker):
    drafts = chunker(fixture)
    assert_no_loss(fixture, drafts)
    assert all(len(d.text) <= 500 for d in drafts)


def test_chunking_defaults_align_with_config():
    # 纯函数默认值与 config 单一事实来源对齐（漂移即测试失败）
    assert Settings().kb_chunk_max_chars == CHUNK_DEFAULT_MAX_CHARS == 500
