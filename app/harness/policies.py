"""
Harness 确定性策略层（Policies）

Harness 工程的核心思想：把「不需要 LLM 判断」的教学理念规则从编排器/Agent 中抽离，
以纯函数形式集中管理，保证：
  1. 教学理念在 LLM 失效时依然由规则兜底（降级策略）
  2. LLM 输出之后的教学约束（强制延续、话题切换、回复组装）统一收口
  3. 所有规则可独立单测

核心逻辑移植自原项目 chains.py / agent.py（教学理念不变）。
"""

import logging
import re
import time
from typing import Optional

from app.models.domain import (
    ClassificationResult,
    CognitiveLevel,
    CognitiveState,
    GuidanceAction,
    GuidanceMode,
    LearningDesireLevel,
    PerceptionResult,
    QuestionType,
    ReflectionResult,
    StepEvaluationResult,
    StepItem,
    StepPlan,
    UserProfile,
)

logger = logging.getLogger(__name__)


def log_fallback(agent_name: str, error: Exception):
    """统一的降级日志格式（移植）"""
    logger.warning(f"[降级] {agent_name} LLM 调用失败，使用降级策略: {error}")


# ============================================================
# 类型映射
# ============================================================

QUESTION_TYPE_TO_MODE = {
    QuestionType.TEACHING: GuidanceMode.EXAMPLE_REPRODUCTION,
    QuestionType.PROBLEM_SOLVING: GuidanceMode.SOCRATIC_QUESTIONING,
    QuestionType.FACTUAL: GuidanceMode.DIRECT_ANSWER,
}

LEVEL_MAP = {
    "remember": CognitiveLevel.REMEMBER,
    "understand": CognitiveLevel.UNDERSTAND,
    "apply": CognitiveLevel.APPLY,
    "analyze": CognitiveLevel.ANALYZE,
    "evaluate": CognitiveLevel.EVALUATE,
    "create": CognitiveLevel.CREATE,
}

DESIRE_MAP = {
    "eager": LearningDesireLevel.EAGER,
    "troubled": LearningDesireLevel.TROUBLED,
    "indifferent": LearningDesireLevel.INDIFFERENT,
}

TYPE_MAP = {
    "teaching": QuestionType.TEACHING,
    "problem_solving": QuestionType.PROBLEM_SOLVING,
    "factual": QuestionType.FACTUAL,
}


# ============================================================
# 会话连续性策略（教学理念：跨轮次跟踪同一问题）
# ============================================================

def check_continuation_signals(user_input: str) -> bool:
    """基于规则判断用户输入是否包含延续对话的信号（移植）"""
    text = user_input.lower()
    signals = [
        "还是不行", "还是没有", "还是那个", "还是没法", "还是解决不了",
        "还是不对", "还是报错", "还是不知道",
        "不行", "还是卡在", "又报了",
        "继续", "接着", "然后呢", "接下来", "下一步",
        "我试了", "我改了", "我重新", "我按照你说的",
        "对，就是", "嗯，那个", "是的，",
        "我懂了", "我理解了", "我明白了", "明白了", "理解了",
        "我做了", "我完成了", "我写出来了", "做好了", "完成了",
        "会了", "没问题", "我试试", "我写了",
        "我觉得", "我认为", "应该是", "可能是", "我猜",
        "我的代码", "我的答案", "这样对吗", "对不对",
        "卡住了", "不懂", "不明白", "不理解",
    ]
    return any(s in text for s in signals)


def detect_topic_switch(user_input: str, step_plan: Optional[StepPlan]) -> bool:
    """检测用户是否明确切换了话题（移植自 agent.py）"""
    if not step_plan:
        return False

    text = user_input.strip().lower()
    switch_signals = [
        "换个话题", "换个问题", "不想学这个了", "换个主题",
        "我想问另一个", "问点别的", "不学这个了", "换个内容",
        "换一个", "聊点别的", "不想讨论这个了",
    ]
    if any(s in text for s in switch_signals):
        return True

    if len(text) <= 30:
        return False

    answer_patterns = [
        "我懂了", "我理解", "我明白", "我试试", "我写",
        "我做了", "我完成了", "我改", "我按照", "我尝试",
        "我的代码", "我的答案", "我觉得", "我认为", "我猜",
        "应该是", "可能是", "因为", "所以", "这样对吗",
        "对不对", "是不是", "然后呢", "下一步",
        "还是不行", "还是不懂", "卡住了", "不会",
    ]
    if any(p in text for p in answer_patterns):
        return False
    return False


# ============================================================
# 诊断后策略（LLM 分类结果的教学约束收口）
# ============================================================

def apply_diagnosis_policies(
    session_id: str,
    user_message: str,
    perception: PerceptionResult,
    classification: ClassificationResult,
    has_active_step_plan: bool,
    active_plan: Optional[StepPlan],
):
    """
    诊断后的确定性教学策略（移植自 agent.process 中分类结果的处理逻辑）：

    1. 有活跃步骤计划且未明确切换话题 → 强制标记为延续对话（步骤计划优先）
    2. 延续对话 → 问题轮数 +1
    3. 非延续 → 清理残留/过期的步骤计划，登记新的活跃问题
    """
    is_topic_switch = detect_topic_switch(user_message, active_plan)

    if has_active_step_plan and not is_topic_switch:
        if not classification.is_continuation:
            logger.info(
                f"步骤计划活跃，强制标记为延续对话"
                f"（分类判定为 {classification.question_type.value}，但不切换步骤计划）"
            )
        classification.is_continuation = True
        if active_plan and active_plan.question_type:
            classification.question_type = active_plan.question_type

    return is_topic_switch, classification


# ============================================================
# 规则降级分类器（LLM 失效时保障教学理念的三分法，移植）
# ============================================================

def rule_based_classify(perception: PerceptionResult) -> ClassificationResult:
    """基于规则的降级分类（核心判据：检测学生学习意愿/情绪）"""
    text = perception.raw_input.lower()
    detected_emotions: list[str] = []

    # ===== Layer 1: 主动学习意愿情绪（eager → teaching）=====
    curiosity_emotions = [
        "是什么样的", "是怎样的", "长什么样", "什么样子",
        "到底是啥", "到底什么是", "具体是怎么",
        "到底是怎么回事", "到底是为什么",
        "是怎么一回事", "究竟是怎么回事", "究竟是怎么",
    ]
    desire_emotions = [
        "怎么学", "教我", "帮我学", "入门", "从零",
        "系统学", "系统地学", "深入理解", "实践",
        "想学", "我想学", "学好", "带我一波", "带我",
    ]
    explore_emotions = [
        "如何理解", "帮我理解", "原理", "为什么是这样",
        "本质", "底层", "机制",
    ]
    passion_emotions = ["想练手", "想自己试", "想试试", "自己动手"]
    general_learning_words = ["理解", "学习", "掌握"]

    # ===== Layer 2: 被动学习意愿情绪（troubled → problem_solving）=====
    anxiety_emotions = [
        "为什么不对", "哪里错了", "报错", "报了", "bug", "解决",
        "帮我看看", "算不出来", "出错", "失败", "错误",
        "异常", "exception", "error", "fault",
        "崩溃", "挂了", "宕机",
        "返回500", "返回404", "500错误", "404错误",
        "oom", "内存溢出", "内存泄漏", "泄漏",
    ]
    confusion_emotions = [
        "不对", "搞不懂", "为什么答案", "为什么是",
        "不知道怎么", "看不出", "找不到", "不明白为什么",
        "明明...却", "明明", "又没问题",
    ]
    frustration_emotions = [
        "卡住", "还是不行", "搞不定",
        "超时", "还是超时", "tle",
        "一直", "重启", "白屏", "拦截", "冲突",
        "还是报错", "还是不对", "还是没法", "反复",
        "怎么办", "怎么解决", "怎么处理",
    ]
    frustration_combos = [
        ("写了", "还是"), ("试了", "还是"), ("试了", "都不行"),
        ("跑着", "就"), ("本地", "没问题"),
    ]

    # ===== Layer 3: 无学习意愿情绪（indifferent → factual）=====
    cold_lookup_signals = [
        "天气", "时间", "日期", "多少", "什么时候", "在哪里", "是谁",
        "区别", "不同", "差异", "定义", "含义",
        "有哪些", "版本", "发布", "年份", "语法",
        "怎么用", "用法", "API", "参数", "返回值",
    ]

    # ---- 学习意愿评分 ----
    desire_score = 0.0
    desire_reasons: list[str] = []

    curiosity_hits = sum(1 for s in curiosity_emotions if s in text)
    if curiosity_hits > 0:
        desire_score += curiosity_hits * 1.5
        desire_reasons.append("好奇探索")
        detected_emotions.append("好奇")

    desire_hits = sum(1 for s in desire_emotions if s in text)
    if desire_hits > 0:
        desire_score += desire_hits * 1.2
        desire_reasons.append("求知渴望")
        detected_emotions.append("渴望")

    explore_hits = sum(1 for s in explore_emotions if s in text)
    if explore_hits > 0:
        desire_score += explore_hits * 1.0
        desire_reasons.append("探究欲望")
        detected_emotions.append("探究欲")

    passion_hits = sum(1 for s in passion_emotions if s in text)
    if passion_hits > 0:
        desire_score += passion_hits * 1.0
        desire_reasons.append("实践热情")
        detected_emotions.append("热情")

    general_hits = sum(1 for s in general_learning_words if s in text)
    if general_hits > 0:
        desire_score += general_hits * 0.5

    if "如何" in text and any(v in text for v in ["学习", "理解", "掌握", "入门", "学好"]):
        desire_score += 1.0
        desire_reasons.append("如何+学习动词")
        if "探究欲" not in detected_emotions:
            detected_emotions.append("求知")

    if "帮我" in text and any(v in text for v in ["理解", "学习", "掌握"]):
        desire_score += 1.0
        if "求知渴望" not in desire_reasons:
            desire_reasons.append("请求理解")
        if "渴望" not in detected_emotions:
            detected_emotions.append("渴望")

    # ---- 无学习意愿评分 ----
    no_desire_score = 0.0
    no_desire_reasons: list[str] = []

    cold_hits = sum(1 for s in cold_lookup_signals if s in text)
    if cold_hits > 0:
        no_desire_score += cold_hits * 0.5
        no_desire_reasons.append("冷淡查证")
        detected_emotions.append("冷淡")

    if "什么是" in text and desire_score < 1.0:
        no_desire_score += 1.0
        no_desire_reasons.append("功利获取")
        if "冷淡" not in detected_emotions:
            detected_emotions.append("冷淡")

    if ("是什么" in text and desire_score < 1.0
            and "是什么样的" not in text and "怎么回事" not in text):
        no_desire_score += 1.0
        no_desire_reasons.append("功利获取")
        if "冷淡" not in detected_emotions:
            detected_emotions.append("冷淡")

    # ---- 被动学习意愿评分 ----
    troubled_score = 0.0
    troubled_reasons: list[str] = []

    anxiety_hits = sum(1 for s in anxiety_emotions if s in text)
    if anxiety_hits > 0:
        troubled_score += anxiety_hits * 1.2
        troubled_reasons.append("焦虑求助")
        detected_emotions.append("焦虑")

    confusion_hits = sum(1 for s in confusion_emotions if s in text)
    if confusion_hits > 0:
        troubled_score += confusion_hits * 1.0
        troubled_reasons.append("困惑")
        if "困惑" not in detected_emotions:
            detected_emotions.append("困惑")

    frustration_hits = sum(1 for s in frustration_emotions if s in text)
    if frustration_hits > 0:
        troubled_score += frustration_hits * 1.0
        troubled_reasons.append("沮丧")
        if "沮丧" not in detected_emotions:
            detected_emotions.append("沮丧")

    combo_hits = sum(1 for (a, b) in frustration_combos if a in text and b in text)
    if combo_hits > 0:
        troubled_score += combo_hits * 1.5
        troubled_reasons.append("反复受挫")
        if "沮丧" not in detected_emotions:
            detected_emotions.append("沮丧")

    # ---- 错误上下文优先级规则 ----
    error_context_signals = [
        "报错", "报了", "错误", "异常", "exception", "error",
        "失败", "崩溃", "fault", "超时", "oom",
        "500", "404", "白屏", "死锁", "泄漏", "拦截",
    ]
    has_error_context = any(s in text for s in error_context_signals)
    if has_error_context and desire_score > 0:
        desire_score *= 0.3
        troubled_score += 1.0
        if "焦虑求助" not in troubled_reasons:
            troubled_reasons.append("错误上下文")
        if "焦虑" not in detected_emotions:
            detected_emotions.append("焦虑")

    # ---- 三路比较 → 学习意愿等级判定 ----
    if desire_score > no_desire_score and desire_score > troubled_score:
        learning_desire_score = min(0.7 + desire_score * 0.05, 1.0)
        reason = f"检测到主动学习意愿({'+'.join(desire_reasons)}), 情绪得分={desire_score:.1f}"
        if not detected_emotions:
            detected_emotions = ["好奇", "渴望"]
        return ClassificationResult(
            question_type=QuestionType.TEACHING,
            learning_desire_level=LearningDesireLevel.EAGER,
            learning_desire_score=learning_desire_score,
            detected_emotions=detected_emotions,
            confidence=min(0.5 + desire_score * 0.1, 0.95),
            reasoning=reason,
            is_continuation=perception.is_continuation,
        )
    elif troubled_score > 0 and troubled_score >= desire_score:
        learning_desire_score = min(0.3 + troubled_score * 0.05, 0.7)
        reason = f"检测到困难情绪({'+'.join(troubled_reasons)}), 急迫感>学习意愿"
        if not detected_emotions:
            detected_emotions = ["焦虑", "困惑"]
        return ClassificationResult(
            question_type=QuestionType.PROBLEM_SOLVING,
            learning_desire_level=LearningDesireLevel.TROUBLED,
            learning_desire_score=learning_desire_score,
            detected_emotions=detected_emotions,
            confidence=min(0.5 + troubled_score * 0.1, 0.95),
            reasoning=reason,
            is_continuation=perception.is_continuation,
        )
    else:
        learning_desire_score = min(0.1 + no_desire_score * 0.03, 0.3)
        reason = (
            f"无学习意愿情绪({'+'.join(no_desire_reasons) if no_desire_reasons else '默认'}), "
            f"冷淡查证"
        )
        if not detected_emotions:
            detected_emotions = ["冷淡"]
        return ClassificationResult(
            question_type=QuestionType.FACTUAL,
            learning_desire_level=LearningDesireLevel.INDIFFERENT,
            learning_desire_score=learning_desire_score,
            detected_emotions=detected_emotions,
            confidence=min(0.5 + no_desire_score * 0.1, 0.95),
            reasoning=reason,
            is_continuation=perception.is_continuation,
        )


# ============================================================
# 轻量评估（factual 跳过 LLM 评估，教学理念：事实性不需要认知评估，移植）
# ============================================================

def lightweight_assess(
    perception: PerceptionResult,
    classification: ClassificationResult,
    prev_state: Optional[CognitiveState] = None,
    user_profile: Optional[UserProfile] = None,
) -> CognitiveState:
    """factual 类型的轻量级评估，不调用 LLM，节省 2-3s 延迟"""
    engagement = prev_state.engagement_level if prev_state else (
        user_profile.avg_engagement if user_profile else 0.5
    )
    frustration = prev_state.frustration_level if prev_state else (
        user_profile.avg_frustration if user_profile else 0.0
    )
    return CognitiveState(
        level=CognitiveLevel.REMEMBER,
        confidence_score=0.7,
        knowledge_gaps=user_profile.weak_topics[-3:] if user_profile else [],
        learning_style=user_profile.preferred_learning_style if user_profile else "visual",
        engagement_level=engagement,
        frustration_level=frustration,
    )


# ============================================================
# 启发式评估降级（LLM 评估链失效时，移植）
# ============================================================

def heuristic_assess(
    perception: PerceptionResult,
    classification: ClassificationResult,
    prev_state: Optional[CognitiveState] = None,
    user_profile: Optional[UserProfile] = None,
) -> CognitiveState:
    base_level = {
        QuestionType.FACTUAL: CognitiveLevel.REMEMBER,
        QuestionType.TEACHING: CognitiveLevel.UNDERSTAND,
        QuestionType.PROBLEM_SOLVING: CognitiveLevel.APPLY,
    }
    engagement = prev_state.engagement_level if prev_state else (
        user_profile.avg_engagement if user_profile else 0.7
    )
    frustration = prev_state.frustration_level if prev_state else (
        user_profile.avg_frustration if user_profile else 0.0
    )

    if classification.is_continuation:
        frustration = min(1.0, frustration + 0.1)

    if perception.emotional_tone in ("confused", "frustrated", "anxious"):
        frustration = min(1.0, frustration + 0.15)
        engagement = max(0.0, engagement - 0.1)
    elif perception.emotional_tone in ("curious", "excited", "confident"):
        engagement = min(1.0, engagement + 0.1)
        frustration = max(0.0, frustration - 0.1)

    return CognitiveState(
        level=base_level.get(classification.question_type, CognitiveLevel.UNDERSTAND),
        confidence_score=0.5,
        knowledge_gaps=user_profile.weak_topics if user_profile else [],
        learning_style=user_profile.preferred_learning_style if user_profile else "visual",
        engagement_level=engagement,
        frustration_level=frustration,
    )


# ============================================================
# 启发式步骤评估降级（教学理念：「宁可推进也不要卡住」，移植）
# ============================================================

def heuristic_step_evaluate(user_input: str, current_step: StepItem) -> StepEvaluationResult:
    text = user_input.lower()

    strong_complete_signals = [
        "我懂了", "明白了", "理解了", "我知道", "我会了",
        "完成了", "做好了", "我写出来了", "没问题",
        "我明白了", "原来如此", "懂了", "会了",
        "搞懂了", "搞明白了", "弄懂了", "弄明白了",
        "理解了", "学会了", "掌握了", "搞定了",
    ]
    medium_signals = [
        "我试一下", "应该是", "我觉得", "我想到了",
        "可能是", "我认为", "我猜", "我的代码",
        "我的答案", "我写了",
    ]
    negative_signals = [
        "不懂", "不明白", "不理解", "还是不行", "还是不会",
        "卡住了", "不知道", "没思路", "搞不懂", "什么意思",
        "还是不懂", "还是不明白",
    ]

    strong = sum(1 for s in strong_complete_signals if s in text)
    medium = sum(1 for s in medium_signals if s in text)
    neg = sum(1 for s in negative_signals if s in text)

    is_detailed = len(user_input) > 50
    # 深度优化：收紧代码检测——裸 "{" / "=>" 等符号在日常文本误报率高，
    # 改为「强代码关键词 或（花括号成对出现）」的保守判据
    has_code = any(
        kw in text for kw in [
            "def ", "class ", "print(", "return ", "#include",
            "import ", "for ", "while ", "function ",
        ]
    ) or ("{" in text and "}" in text)

    if strong > 0 or has_code or (is_detailed and neg == 0):
        return StepEvaluationResult(
            step_completed=True,
            student_understood=0.8 if strong > 0 else 0.6,
            should_advance=True,
        )
    elif medium > 0 and neg == 0:
        return StepEvaluationResult(step_completed=True, student_understood=0.6, should_advance=True)
    elif neg > 0 and medium == 0 and strong == 0:
        return StepEvaluationResult(
            step_completed=False,
            student_understood=0.2,
            should_advance=False,
            stay_reason="学生表示不理解",
            hint_to_provide=current_step.hint if current_step.hint else None,
        )
    else:
        return StepEvaluationResult(step_completed=True, student_understood=0.5, should_advance=True)


# ============================================================
# 降级步骤计划（LLM 步骤计划生成失败时，移植）
# ============================================================

def fallback_step_plan(
    perception: PerceptionResult,
    classification: ClassificationResult,
) -> Optional[StepPlan]:
    """降级步骤计划生成（根据问题复杂度灵活调整步骤数）"""
    topic = perception.raw_input[:50]
    text = perception.raw_input.lower()
    is_complex = any(
        kw in text for kw in ["递归", "面向对象", "设计模式", "架构", "优化", "排序算法", "数据结构"]
    )
    is_simple = any(kw in text for kw in ["变量", "语法", "类型", "格式", "安装", "配置"])

    if classification.question_type == QuestionType.TEACHING:
        if is_simple:
            steps = [
                StepItem(index=1, title="展示示例", description=f"先看一个关于「{topic}」的简单示例", hint="观察示例中的关键步骤"),
                StepItem(index=2, title="模仿复现", description="尝试自己复现这个示例", hint="按照示例的步骤一步一步来"),
            ]
        elif is_complex:
            steps = [
                StepItem(index=1, title="展示示例", description=f"先看一个关于「{topic}」的简单示例", hint="观察示例中的关键步骤"),
                StepItem(index=2, title="模仿复现", description="尝试自己复现这个示例", hint="按照示例的步骤一步一步来"),
                StepItem(index=3, title="修改变式", description="在示例基础上做一点修改", hint="改变其中一个参数试试"),
                StepItem(index=4, title="应用拓展", description="把学到的应用到新场景", hint="想想还有哪些类似的情况"),
                StepItem(index=5, title="独立实践", description="独立完成一个相关任务", hint="回忆前面的步骤，自主应用"),
            ]
        else:
            steps = [
                StepItem(index=1, title="展示示例", description=f"先看一个关于「{topic}」的简单示例", hint="观察示例中的关键步骤"),
                StepItem(index=2, title="模仿复现", description="尝试自己复现这个示例", hint="按照示例的步骤一步一步来"),
                StepItem(index=3, title="修改变式", description="在示例基础上做一点修改", hint="改变其中一个参数试试"),
                StepItem(index=4, title="应用拓展", description="把学到的应用到新场景", hint="想想还有哪些类似的情况"),
            ]
        return StepPlan(
            plan_id=f"plan-{int(time.time() * 1000)}",
            topic=topic,
            question_type=QuestionType.TEACHING,
            guidance_mode=GuidanceMode.EXAMPLE_REPRODUCTION,
            steps=steps,
            created_at=time.time(),
            updated_at=time.time(),
        )

    if classification.question_type == QuestionType.PROBLEM_SOLVING:
        if is_simple:
            steps = [
                StepItem(index=1, title="确认问题", description="你能描述一下具体遇到了什么问题吗？", hint="从已知信息开始"),
                StepItem(index=2, title="尝试解决", description="你觉得可能的解决方向是什么？", hint="尝试排除不可能的方向"),
            ]
        elif is_complex:
            steps = [
                StepItem(index=1, title="确认已知", description="你目前对这个问题了解多少？", hint="从已知信息开始"),
                StepItem(index=2, title="关注关键", description="问题的关键特征是什么？", hint="关注核心矛盾"),
                StepItem(index=3, title="提示方向", description="你觉得可能的解决方向是什么？", hint="尝试排除不可能的方向"),
                StepItem(index=4, title="推导验证", description="能否验证你的结论？", hint="用另一种方式检查"),
                StepItem(index=5, title="总结推广", description="这个解决方法还能用在哪些类似问题上？", hint="思考方法的通用性"),
            ]
        else:
            steps = [
                StepItem(index=1, title="确认已知", description="你目前对这个问题了解多少？", hint="从已知信息开始"),
                StepItem(index=2, title="关注关键", description="问题的关键特征是什么？", hint="关注核心矛盾"),
                StepItem(index=3, title="提示方向", description="你觉得可能的解决方向是什么？", hint="尝试排除不可能的方向"),
                StepItem(index=4, title="推导验证", description="能否验证你的结论？", hint="用另一种方式检查"),
            ]
        return StepPlan(
            plan_id=f"plan-{int(time.time() * 1000)}",
            topic=topic,
            question_type=QuestionType.PROBLEM_SOLVING,
            guidance_mode=GuidanceMode.SOCRATIC_QUESTIONING,
            steps=steps,
            created_at=time.time(),
            updated_at=time.time(),
        )
    return None


# ============================================================
# 降级引导（LLM 引导失败时，移植）
# ============================================================

def fallback_guide(
    perception: PerceptionResult,
    classification: ClassificationResult,
    cognitive_state: Optional[CognitiveState] = None,
    active_plan: Optional[StepPlan] = None,
    is_step_complete: bool = False,
) -> GuidanceAction:
    """
    降级引导（LLM 引导失败时，移植 + 深度优化）

    深度优化：感知步骤计划上下文——原版降级引导与当前步骤完全脱节
    （实测：步骤 3/4「修改变式」时降级回复仍是「让我展示一个示例」开头），
    现按步骤上下文生成贴切的降级引导：
      - 计划全部完成 → 庆祝 + 知识拓展模式（对齐教学理念）
      - 活跃计划 → 围绕当前步骤引导（标题/描述/提示）
      - 无计划 → 原版三分法降级文案
    """
    encouragement = None
    if cognitive_state and cognitive_state.frustration_level > 0.3:
        encouragement = "别着急，我们一步步来，每解决一个小问题都是进步。"

    mode = QUESTION_TYPE_TO_MODE.get(
        classification.question_type, GuidanceMode.SOCRATIC_QUESTIONING
    )

    # ---- 步骤计划全部完成：庆祝 + 知识拓展模式（教学理念：完成时庆祝） ----
    if is_step_complete:
        return GuidanceAction(
            mode=mode,
            content=(
                "🎉 恭喜！你已经完成了全部学习步骤，这个知识点你已经掌握了。\n\n"
                "📚 知识拓展：试着把这个知识点和之前学过的内容联系起来，"
                "或者找一个新场景独立应用一次——能向别人讲清楚，才是真正掌握。"
            ),
            encouragement=encouragement,
            is_continuation=classification.is_continuation,
        )

    # ---- 活跃步骤计划：围绕当前步骤生成贴切引导 ----
    if active_plan and active_plan.current_step:
        step = active_plan.current_step
        hint_suffix = f"\n提示：{step.hint}" if step.hint else ""
        if mode == GuidanceMode.EXAMPLE_REPRODUCTION:
            content = (
                f"现在进入「{step.title}」：{step.description}。"
                f"{hint_suffix}"
            )
        elif mode == GuidanceMode.SOCRATIC_QUESTIONING:
            content = (
                f"关于当前步骤「{step.title}」——{step.description}。"
                f"你觉得这一步的关键在哪里？可以先说说你的想法，我们再一起验证。"
                f"{hint_suffix}"
            )
        else:
            content = f"关于当前步骤「{step.title}」：{step.description}。{hint_suffix}"
        return GuidanceAction(
            mode=mode,
            content=content,
            encouragement=encouragement,
            is_continuation=classification.is_continuation,
        )

    # ---- 无步骤计划：原版三分法降级 ----
    if classification.question_type == QuestionType.TEACHING:
        return GuidanceAction(
            mode=GuidanceMode.EXAMPLE_REPRODUCTION,
            content=(
                f"让我给你展示一个关于「{perception.raw_input[:30]}」的示例。"
                "看完示例后，你可以尝试自己复现一遍，这样理解会更深刻。"
            ),
            encouragement=encouragement,
            is_continuation=classification.is_continuation,
        )
    elif classification.question_type == QuestionType.PROBLEM_SOLVING:
        return GuidanceAction(
            mode=GuidanceMode.SOCRATIC_QUESTIONING,
            content=(
                "这是一个很有意思的问题。让我问你：你对这个问题目前有什么想法？"
                "我们可以从你已经知道的部分开始。"
            ),
            encouragement=encouragement,
            is_continuation=classification.is_continuation,
        )
    return GuidanceAction(
        mode=GuidanceMode.DIRECT_ANSWER,
        content="关于你的问题，让我直接为你解答。",
        is_continuation=classification.is_continuation,
    )


# ============================================================
# 贴切鼓励语生成器（确定性规则，教学理念：贴切而非空洞，移植）
# ============================================================

def generate_encouragement(
    cognitive_state: CognitiveState,
    classification: ClassificationResult,
    user_profile: Optional[UserProfile],
) -> Optional[str]:
    """生成贴切的鼓励语（移植原项目逻辑，原样保留）"""
    if cognitive_state.frustration_level > 0.5:
        if classification.is_continuation:
            return "虽然这个问题确实有难度，但你一直在坚持尝试，这本身就是最宝贵的学习品质。我们换个角度再看看——"
        if user_profile and user_profile.weak_topics:
            weak = user_profile.weak_topics[-1]
            return (
                f"关于{weak}确实需要多花些时间，这很正常。"
                "很多同学在这个知识点上也会反复几次才真正理解，我们一步步来。"
            )

    if cognitive_state.frustration_level > 0.3:
        if classification.question_type == QuestionType.PROBLEM_SOLVING:
            return (
                "调试和排错是编程中最常见的工作，每一次报错都是在帮你更深入地理解代码。"
                "你离答案可能只差一个思路的转换。"
            )
        return "学习新东西遇到瓶颈是正常的，这说明你正在突破舒适区。再坚持一下，往往突破就在下一步。"

    if cognitive_state.engagement_level > 0.7:
        if classification.question_type == QuestionType.TEACHING:
            return "你的学习态度很积极！保持这种好奇心，对学计算机来说这是最重要的品质。"
        if classification.question_type == QuestionType.PROBLEM_SOLVING:
            return "你正在用很好的方式思考这个问题！在计算机学习中，主动思考比直接看答案有效得多。"

    if cognitive_state.level in (CognitiveLevel.APPLY, CognitiveLevel.ANALYZE):
        return "你已经能开始应用和分析了，说明理解正在从表面走向深入，继续保持！"

    return None


# ============================================================
# 启发式反思降级（移植）
# ============================================================

def heuristic_reflect(
    cognitive_state: CognitiveState,
    classification: Optional[ClassificationResult] = None,
) -> ReflectionResult:
    effectiveness = 0.5
    adjustments: list[str] = []
    profile_updates: dict = {}

    if cognitive_state.engagement_level > 0.7:
        effectiveness += 0.2
    elif cognitive_state.engagement_level < 0.3:
        effectiveness -= 0.2

    if cognitive_state.frustration_level > 0.5:
        effectiveness -= 0.2
        adjustments.append("降低问题难度，先建立信心")
        profile_updates["preferredDifficulty"] = "easy"
    if cognitive_state.frustration_level > 0.7:
        adjustments.append("暂时切换到更简单的话题")

    if cognitive_state.frustration_level < 0.2 and cognitive_state.engagement_level > 0.7:
        profile_updates["addPersonalityTags"] = ["学习投入度高"]

    return ReflectionResult(
        strategy_effectiveness=max(0.0, min(1.0, effectiveness)),
        student_progress=0.5,
        adjustments=adjustments,
        profile_updates=profile_updates,
    )


# ============================================================
# Prompt 片段构建（移植）
# ============================================================

def build_user_profile_section(profile: Optional[UserProfile]) -> str:
    """构建用户画像的 Prompt 片段"""
    if not profile:
        return ""
    parts = ["【学生画像】"]
    if profile.identity:
        parts.append(f"  身份：{profile.identity}")
        basic_bits = []
        if profile.grade:
            basic_bits.append(f"年级：{profile.grade}")
        if profile.major:
            basic_bits.append(f"专业：{profile.major}")
        if basic_bits:
            parts.append("  " + "，".join(basic_bits))
    else:
        parts.append("  身份：尚未了解（请在对话中自然了解学生背景，不要预设）")
    if profile.known_topics:
        parts.append(f"  已掌握知识点：{', '.join(profile.known_topics[-8:])}")
    if profile.weak_topics:
        parts.append(f"  薄弱知识点：{', '.join(profile.weak_topics[-8:])}")
    if profile.interest_topics:
        parts.append(f"  感兴趣领域：{', '.join(profile.interest_topics[-5:])}")
    parts.append(f"  学习风格偏好：{profile.preferred_learning_style}")
    parts.append(f"  鼓励方式偏好：{profile.preferred_encouragement_style}")
    parts.append(f"  难度偏好：{profile.preferred_difficulty}")
    if profile.personality_tags:
        parts.append(f"  个性标签：{', '.join(profile.personality_tags)}")
    if profile.effective_strategies:
        parts.append(f"  有效的策略：{', '.join(profile.effective_strategies[-5:])}")
    if profile.ineffective_strategies:
        parts.append(f"  效果不好的策略：{', '.join(profile.ineffective_strategies[-5:])}")
    parts.append(f"  平均参与度：{profile.avg_engagement:.2f}")
    parts.append(f"  平均挫败感：{profile.avg_frustration:.2f}")
    return "\n".join(parts)


def build_continuation_section(
    is_continuation: bool,
    unresolved_info: Optional[dict] = None,
) -> str:
    """构建会话连续性的 Prompt 片段"""
    if not is_continuation or not unresolved_info:
        return ""

    parts = ["【⚠️ 这是一个延续之前未解决问题的对话】"]
    parts.append(f"  之前的问题：{unresolved_info.get('topic', '未知')}")
    parts.append(f"  已交互轮数：{unresolved_info.get('turn_count', 0)}")

    question_type = unresolved_info.get("question_type")
    if question_type:
        parts.append(
            f"  问题类型：{question_type.value if hasattr(question_type, 'value') else question_type}"
        )

    last_interaction = unresolved_info.get("last_interaction", "")
    if last_interaction:
        parts.append(f"  最近交互：\n{last_interaction}")

    parts.append("")
    parts.append("重要提醒：必须衔接上次的引导思路，不要从头开始！")
    return "\n".join(parts)


def build_strategy_hints_section(hints: list[str]) -> str:
    """构建策略提示的 Prompt 片段（Harness 新增：策略经验注入）"""
    if not hints:
        return ""
    parts = ["【历史策略经验（供参考）】"]
    for h in hints:
        parts.append(f"  - {h}")
    return "\n".join(parts)


def build_experience_hints_section(hints: list[str]) -> str:
    """构建教学经验提示片段（v5.0：跨用户统一整理的教学洞见注入）"""
    if not hints:
        return ""
    parts = ["【教学经验库（历代学习者验证过的高效做法，供参考）】"]
    for h in hints:
        parts.append(f"  - {h}")
    return "\n".join(parts)


def build_step_completion_section(is_step_complete: bool) -> str:
    """构建步骤完成标记段落（触发庆祝+知识拓展模式）"""
    if not is_step_complete:
        return ""
    return (
        "\n【🎉 步骤计划已全部完成！isStepComplete=true】\n"
        "学生已经完成了所有教学/解答步骤，已经掌握了本次学习的知识。\n"
        "你必须切换为\"学习成就庆祝+知识拓展\"模式，按照Prompt中的指示输出。\n"
    )


# ============================================================
# 回复组装（移植自 agent.py _compose_reply）
# ============================================================

# v4.3 修复：LLM 会从历史回复中学样，自行输出「📌 x/y：标题」或
# 「【步骤进度 x/y】」样式的进度行，与系统侧/前端渲染重复。
# 该正则用于剥离这些模仿行（仅匹配以进度标记开头的行，不会误伤正文）。
_LLM_PROGRESS_LINE_RE = re.compile(
    r"^[ \t]*(?:"
    r"[\U0001F4CC\U0001F4CD][ \t]*\d+\s*/\s*\d+[ \t]*[\uff1a:]?[ \t]*"  # 📌 4/5：xxx / 📌 4/5 xxx
    r"|\u3010[ \t]*\u6b65\u9aa4\u8fdb\u5ea6[ \t]*[^\n]*\u3011[ \t]*"  # 【步骤进度 4/5】xxx
    r")[^\n]*\n?",
    re.MULTILINE,
)


def strip_llm_progress_lines(content: str) -> str:
    """剥离 LLM 自行模仿输出的进度行（v4.3 新增，可独立单测）

    进度展示的唯一来源是前端 step-chip（由 step_progress/
    current_step_title/step_plan 结构化字段驱动），正文中的任何
    进度行都属重复，统一剥除。
    """
    if not content:
        return content
    cleaned = _LLM_PROGRESS_LINE_RE.sub("", content)
    if cleaned == content:
        return content
    # 剥离后清理头部空行；若剥离后为空（内容只有进度行）则保守返回原文，
    # 保证最终回复非空（宁可保留一行冗余也不能输出空回复）
    return cleaned.lstrip("\n") or content


def compose_reply(
    guidance: GuidanceAction,
    question_type: QuestionType,
    step_plan: Optional[StepPlan] = None,
    is_step_complete: bool = False,
) -> str:
    """组装最终回复（凝练简洁，避免冗余）

    v4.3 回复卫生修复（教学理念不变，展示职责单一化）：
      1. 不再在正文前拼接「📌 x/y：标题」进度行——进度由前端
         step-chip 统一渲染，API 侧保留 step_progress/current_step_title/
         step_plan 结构化字段。旧版正文进度行会被写入历史，导致
         LLM 在后续轮次学样输出，与系统行/前端徽章形成重复。
      2. 不再追加「示例：{plan.example}」——步骤计划的 example 是
         轮1后台反思时生成的，轮2起每轮追加会造成「在鼓励语前
         重复输出第一轮示例」。示例由导师正文自然承载（轮1已展示），
         plan.example 仍通过 API example 字段透出。
      3. 剥离 LLM 自行模仿输出的进度行（strip_llm_progress_lines）。
    """
    parts: list[str] = []

    # 延续对话且步骤未完成时的简洁衔接语
    if guidance.is_continuation and guidance.continuation_context and not is_step_complete:
        parts.append(f"继续「{guidance.continuation_context}」——")

    parts.append(strip_llm_progress_lines(guidance.content))

    # 鼓励语不追加到正文，由前端通过 encouragement 字段单独渲染
    return "\n".join(parts)
