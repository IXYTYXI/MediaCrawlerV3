# -*- coding: utf-8 -*-
"""
关键词加载器模块

功能：
1. 从配置文件加载分类关键词
2. 支持多种组合模式（单个、组合、模板）
3. 支持随机抽取和排列组合
"""

import json
import random
import re
from pathlib import Path
from typing import List, Dict, Optional, Set
from itertools import product

from tools import utils


class KeywordsLoader:
    """关键词加载器"""
    
    def __init__(self, config_path: str = "config/keywords_config.json"):
        self.config_path = Path(config_path)
        self.config: Dict = {}
        self.all_keywords: Dict[str, Dict[str, List[str]]] = {}
        self._load_config()
    
    def _load_config(self):
        """加载配置文件"""
        if not self.config_path.exists():
            utils.logger.warning(f"[KeywordsLoader] 配置文件不存在: {self.config_path}")
            return
        
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
            
            # 提取关键词类别
            for key, value in self.config.items():
                if key.startswith("_") or key in ["组合规则", "使用设置"]:
                    continue
                if isinstance(value, dict):
                    self.all_keywords[key] = value
            
            utils.logger.info(f"[KeywordsLoader] 加载成功，共 {len(self.all_keywords)} 个类别")
            
        except Exception as e:
            utils.logger.error(f"[KeywordsLoader] 加载失败: {e}")
    
    def get_category_keywords(self, category_path: str) -> List[str]:
        """
        获取指定类别的关键词
        
        Args:
            category_path: 类别路径，如 "品牌名称.数理化理科" 或 "品牌名称.*"
            
        Returns:
            关键词列表
        """
        parts = category_path.split(".")
        
        if len(parts) == 1:
            # 获取整个大类的所有关键词
            category = parts[0]
            if category in self.all_keywords:
                result = []
                for sub_keywords in self.all_keywords[category].values():
                    result.extend(sub_keywords)
                return result
            return []
        
        elif len(parts) == 2:
            category, sub_category = parts
            if category not in self.all_keywords:
                return []
            
            if sub_category == "*":
                # 获取该类别下所有关键词
                result = []
                for sub_keywords in self.all_keywords[category].values():
                    result.extend(sub_keywords)
                return result
            else:
                return self.all_keywords[category].get(sub_category, [])
        
        return []
    
    def get_single_keywords(self, categories: Optional[List[str]] = None, shuffle: bool = True) -> List[str]:
        """
        获取单个关键词列表
        
        Args:
            categories: 指定类别列表，None则使用全部
            shuffle: 是否随机打乱
            
        Returns:
            关键词列表
        """
        result = []
        
        active_categories = categories or self.config.get("使用设置", {}).get("active_categories", [])
        
        if not active_categories:
            # 使用全部类别
            for category in self.all_keywords:
                result.extend(self.get_category_keywords(category))
        else:
            for category in active_categories:
                result.extend(self.get_category_keywords(category))
        
        if shuffle:
            random.shuffle(result)
        
        return result
    
    def generate_combinations(self, categories: List[str], separator: str = " ") -> List[str]:
        """
        生成关键词组合（排列组合）
        
        Args:
            categories: 要组合的类别路径列表
            separator: 分隔符
            
        Returns:
            组合后的关键词列表
        """
        keyword_lists = [self.get_category_keywords(cat) for cat in categories]
        
        # 过滤空列表
        keyword_lists = [lst for lst in keyword_lists if lst]
        
        if not keyword_lists:
            return []
        
        # 生成排列组合
        combinations = list(product(*keyword_lists))
        
        # 拼接成字符串
        result = [separator.join(combo) for combo in combinations]
        
        return result
    
    def generate_from_template(self, template: str) -> List[str]:
        """
        根据模板生成关键词
        
        Args:
            template: 模板字符串，如 "{品牌名称.数理化理科} {场景功能.学段学科}"
            
        Returns:
            生成的关键词列表
        """
        # 找出模板中的所有占位符
        pattern = r'\{([^}]+)\}'
        placeholders = re.findall(pattern, template)
        
        if not placeholders:
            return [template]
        
        # 获取每个占位符对应的关键词列表
        keyword_lists = []
        for placeholder in placeholders:
            keywords = self.get_category_keywords(placeholder)
            if keywords:
                keyword_lists.append(keywords)
            else:
                keyword_lists.append([f"{{{placeholder}}}"])  # 保留原占位符
        
        # 生成组合
        combinations = list(product(*keyword_lists))
        
        # 替换模板
        result = []
        for combo in combinations:
            text = template
            for placeholder, keyword in zip(placeholders, combo):
                text = text.replace(f"{{{placeholder}}}", keyword, 1)
            result.append(text)
        
        return result
    
    def get_keywords(self, mode: Optional[str] = None, limit: Optional[int] = None) -> List[str]:
        """
        获取关键词（主入口）
        
        Args:
            mode: 模式 (single/combination/template/all)，None则使用配置
            limit: 最大数量，None则使用配置
            
        Returns:
            关键词列表
        """
        settings = self.config.get("使用设置", {})
        mode = mode or settings.get("mode", "single")
        limit = limit or settings.get("max_keywords_per_run", 10)
        shuffle = settings.get("shuffle", True)
        
        result = []
        
        if mode == "single":
            result = self.get_single_keywords(shuffle=shuffle)
            
        elif mode == "combination":
            # 使用预设组合
            preset = self.config.get("组合规则", {}).get("preset_combinations", [])
            for combo in preset:
                categories = combo.get("categories", [])
                separator = combo.get("separator", " ")
                result.extend(self.generate_combinations(categories, separator))
            
        elif mode == "template":
            # 使用自定义模板
            templates = self.config.get("组合规则", {}).get("custom_templates", [])
            for template in templates:
                result.extend(self.generate_from_template(template))
            
        elif mode == "all":
            # 所有方式
            result.extend(self.get_single_keywords(shuffle=False))
            
            preset = self.config.get("组合规则", {}).get("preset_combinations", [])
            for combo in preset:
                categories = combo.get("categories", [])
                separator = combo.get("separator", " ")
                result.extend(self.generate_combinations(categories, separator))
            
            templates = self.config.get("组合规则", {}).get("custom_templates", [])
            for template in templates:
                result.extend(self.generate_from_template(template))
        
        # 去重
        result = list(dict.fromkeys(result))
        
        # 打乱
        if shuffle:
            random.shuffle(result)
        
        # 限制数量
        if limit and len(result) > limit:
            result = result[:limit]
        
        utils.logger.info(f"[KeywordsLoader] 生成 {len(result)} 个关键词 (mode={mode})")
        
        return result
    
    def get_keywords_string(self, mode: Optional[str] = None, limit: Optional[int] = None, separator: str = ",") -> str:
        """
        获取关键词字符串（用于直接填入配置）
        
        Args:
            mode: 模式
            limit: 最大数量
            separator: 分隔符
            
        Returns:
            关键词字符串
        """
        keywords = self.get_keywords(mode=mode, limit=limit)
        return separator.join(keywords)
    
    def print_all_keywords(self):
        """打印所有关键词（调试用）"""
        print("\n" + "=" * 60)
        print("关键词配置概览")
        print("=" * 60)
        
        for category, sub_categories in self.all_keywords.items():
            print(f"\n【{category}】")
            for sub_cat, keywords in sub_categories.items():
                print(f"  {sub_cat}: {', '.join(keywords[:5])}{'...' if len(keywords) > 5 else ''}")
        
        print("\n" + "=" * 60)


# 全局实例
_keywords_loader: Optional[KeywordsLoader] = None


def get_keywords_loader() -> KeywordsLoader:
    """获取全局关键词加载器"""
    global _keywords_loader
    if _keywords_loader is None:
        _keywords_loader = KeywordsLoader()
    return _keywords_loader


def get_keywords(mode: Optional[str] = None, limit: Optional[int] = None) -> List[str]:
    """便捷函数：获取关键词列表"""
    return get_keywords_loader().get_keywords(mode=mode, limit=limit)


def get_keywords_string(mode: Optional[str] = None, limit: Optional[int] = None) -> str:
    """便捷函数：获取关键词字符串"""
    return get_keywords_loader().get_keywords_string(mode=mode, limit=limit)


# 命令行测试
if __name__ == "__main__":
    loader = KeywordsLoader()
    loader.print_all_keywords()
    
    print("\n【单个关键词模式】")
    keywords = loader.get_keywords(mode="single", limit=10)
    print(f"  {keywords}")
    
    print("\n【模板模式】")
    keywords = loader.get_keywords(mode="template", limit=10)
    print(f"  {keywords}")
    
    print("\n【组合模式】")
    keywords = loader.get_keywords(mode="combination", limit=10)
    print(f"  {keywords}")
