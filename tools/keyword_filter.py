# -*- coding: utf-8 -*-
"""
关键词解析与笔记过滤模块

支持三种运算符：
  | (OR)  — 命中任一即可
  & (AND) — 必须全部命中
  空格    — 作为整体匹配的组合词

优先级：& > |，空格视为组合词（不拆分）。

示例：
  "小学 数学|网课"     → 含「小学 数学」OR 含「网课」
  "小学&数学&网课"     → 必须同时含「小学」「数学」「网课」
  "小学 数学&提前学"   → 含「小学 数学」AND 含「提前学」
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class FilterScope:
    """过滤范围配置"""
    title: bool = True
    desc: bool = True
    tags: bool = True
    comments: bool = False
    author_desc: bool = False


@dataclass
class AndGroup:
    """AND 组：所有 term 必须全部命中"""
    terms: List[str] = field(default_factory=list)

    def match(self, text: str) -> bool:
        lower = text.lower()
        return all(t.lower() in lower for t in self.terms)


@dataclass
class KeywordExpression:
    """
    关键词表达式：由多个 AndGroup 通过 OR 连接。
    整体匹配逻辑：命中任意一个 AndGroup 即为匹配。
    """
    or_groups: List[AndGroup] = field(default_factory=list)
    raw: str = ""

    def match(self, text: str) -> bool:
        if not self.or_groups:
            return True
        return any(g.match(text) for g in self.or_groups)

    def is_empty(self) -> bool:
        return not self.or_groups


def parse_keyword_expression(expr: str) -> KeywordExpression:
    """
    解析关键词表达式字符串。

    规则：
      1. 先按 | 拆分为多个 OR 分支
      2. 每个 OR 分支内按 & 拆分为多个 AND term
      3. 每个 term 保留空格（作为组合词整体匹配）
      4. 空字符串返回空表达式（匹配所有）

    示例：
      "小学 数学|网课"   → [AndGroup(["小学 数学"]), AndGroup(["网课"])]
      "小学&数学&网课"   → [AndGroup(["小学", "数学", "网课"])]
      "小学 数学&提前学" → [AndGroup(["小学 数学", "提前学"])]
    """
    expr = expr.strip()
    if not expr:
        return KeywordExpression(raw=expr)

    or_groups: List[AndGroup] = []
    for or_part in expr.split("|"):
        or_part = or_part.strip()
        if not or_part:
            continue
        terms = [t.strip() for t in or_part.split("&") if t.strip()]
        if terms:
            or_groups.append(AndGroup(terms=terms))

    return KeywordExpression(or_groups=or_groups, raw=expr)


def parse_multi_keyword_expressions(keywords_str: str) -> List[KeywordExpression]:
    """
    解析逗号分隔的多个关键词表达式。
    每个表达式独立，用于 n×n 笛卡尔积模式。

    "小学&数学, 网课|在线" → [expr1, expr2]
    """
    if not keywords_str or not keywords_str.strip():
        return []
    expressions = []
    for part in keywords_str.split(","):
        part = part.strip()
        if part:
            expressions.append(parse_keyword_expression(part))
    return expressions


def build_note_text(note: Dict, scope: FilterScope) -> str:
    """
    根据过滤范围拼接笔记的待检文本。

    Args:
        note: 笔记详情字典（来自 get_note_by_id 或存储）
        scope: 过滤范围配置
    """
    parts: List[str] = []

    if scope.title:
        title = note.get("title", "") or note.get("display_title", "") or ""
        parts.append(title)

    if scope.desc:
        desc = note.get("desc", "") or ""
        parts.append(desc)

    if scope.tags:
        tag_list = note.get("tag_list", [])
        if isinstance(tag_list, list):
            for tag in tag_list:
                if isinstance(tag, dict):
                    parts.append(tag.get("name", ""))
                elif isinstance(tag, str):
                    parts.append(tag)
        elif isinstance(tag_list, str):
            parts.append(tag_list)

    if scope.comments:
        comments = note.get("_comments", [])
        if isinstance(comments, list):
            for c in comments:
                if isinstance(c, dict):
                    parts.append(c.get("content", ""))
                elif isinstance(c, str):
                    parts.append(c)

    if scope.author_desc:
        user = note.get("user", {})
        if isinstance(user, dict):
            parts.append(user.get("desc", "") or "")

    return "\n".join(parts)


def match_note(
    note: Dict,
    expression: KeywordExpression,
    scope: Optional[FilterScope] = None,
) -> bool:
    """
    判断笔记是否匹配关键词表达式。

    Args:
        note: 笔记详情字典
        expression: 关键词表达式
        scope: 过滤范围（默认 title+desc+tags）

    Returns:
        True 表示匹配
    """
    if expression.is_empty():
        return True
    if scope is None:
        scope = FilterScope()
    text = build_note_text(note, scope)
    return expression.match(text)


def match_note_multi(
    note: Dict,
    expressions: List[KeywordExpression],
    scope: Optional[FilterScope] = None,
) -> List[str]:
    """
    判断笔记匹配了哪些关键词表达式，返回命中的原始关键词列表。

    Args:
        note: 笔记详情字典
        expressions: 关键词表达式列表
        scope: 过滤范围

    Returns:
        命中的关键词原始字符串列表（可能为空）
    """
    if scope is None:
        scope = FilterScope()
    text = build_note_text(note, scope)
    matched: List[str] = []
    for expr in expressions:
        if expr.is_empty():
            continue
        if expr.match(text):
            matched.append(expr.raw)
    return matched
