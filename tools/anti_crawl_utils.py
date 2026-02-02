# -*- coding: utf-8 -*-
"""
高级反反爬工具模块
实现各种反检测策略
"""
import asyncio
import random
import math
from typing import Optional, Tuple, List
from enum import Enum

from tools import utils


class WaitContext(Enum):
    """等待上下文类型"""
    PAGE_LOAD = "page_load"           # 页面加载
    USER_INTERACTION = "user_interaction"  # 用户交互
    API_RETRY = "api_retry"           # API 重试
    COMMENT_FETCH = "comment_fetch"   # 评论获取
    DEFAULT = "default"               # 默认


class DynamicWaitManager:
    """
    动态等待时间管理器
    根据请求成功/失败情况动态调整等待时间
    """
    
    def __init__(self):
        self.consecutive_successes = 0
        self.consecutive_failures = 0
        self.base_multiplier = 1.0
        self.is_in_super_sleep = False
    
    def record_success(self):
        """记录成功请求"""
        self.consecutive_successes += 1
        self.consecutive_failures = 0
        self.is_in_super_sleep = False
    
    def record_failure(self, is_block: bool = False):
        """
        记录失败请求
        Args:
            is_block: 是否是被封锁（验证码/403等）
        """
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        if is_block:
            self.is_in_super_sleep = True
    
    def get_multiplier(self, config) -> float:
        """
        获取当前等待时间乘数
        """
        if not getattr(config, "DYNAMIC_ADJUST_ENABLED", False):
            return 1.0
        
        success_threshold = getattr(config, "DYNAMIC_SUCCESS_THRESHOLD", 10)
        speed_up_factor = getattr(config, "DYNAMIC_SPEED_UP_FACTOR", 0.8)
        slow_down_factor = getattr(config, "DYNAMIC_SLOW_DOWN_FACTOR", 2.0)
        
        if self.consecutive_successes >= success_threshold:
            # 连续成功，可以适当加速
            return max(0.5, speed_up_factor)
        elif self.consecutive_failures > 0:
            # 有失败，减速
            return min(5.0, slow_down_factor ** self.consecutive_failures)
        
        return 1.0
    
    async def maybe_super_sleep(self, config) -> bool:
        """
        如果需要，执行超级休眠
        Returns:
            是否执行了超级休眠
        """
        if not self.is_in_super_sleep:
            return False
        
        if not getattr(config, "DYNAMIC_SUPER_SLEEP_ON_BLOCK", False):
            return False
        
        min_sec = getattr(config, "DYNAMIC_SUPER_SLEEP_MIN_SEC", 300)
        max_sec = getattr(config, "DYNAMIC_SUPER_SLEEP_MAX_SEC", 600)
        sleep_time = random.uniform(min_sec, max_sec)
        
        utils.logger.warning(f"[AntiCrawl] 触发超级休眠，等待 {sleep_time/60:.1f} 分钟...")
        await asyncio.sleep(sleep_time)
        
        self.is_in_super_sleep = False
        self.consecutive_failures = 0
        return True


# 全局动态等待管理器
_wait_manager = DynamicWaitManager()


def get_wait_manager() -> DynamicWaitManager:
    """获取全局动态等待管理器"""
    return _wait_manager


def generate_random_wait(
    min_sec: float,
    max_sec: float,
    distribution: str = "lognormal"
) -> float:
    """
    生成随机等待时间
    
    Args:
        min_sec: 最小等待时间
        max_sec: 最大等待时间
        distribution: 分布类型 (uniform/normal/lognormal)
    
    Returns:
        随机等待时间（秒）
    """
    if distribution == "uniform":
        # 均匀分布
        return random.uniform(min_sec, max_sec)
    
    elif distribution == "normal":
        # 正态分布：大部分值在中间，偶尔出现极值
        mean = (min_sec + max_sec) / 2
        std = (max_sec - min_sec) / 4  # 约 95% 在范围内
        value = random.gauss(mean, std)
        return max(min_sec, min(max_sec, value))
    
    elif distribution == "lognormal":
        # 对数正态分布：大部分值偏小，偶尔出现较长等待
        # 这更符合人类行为模式
        mean = (min_sec + max_sec) / 2
        sigma = 0.5  # 控制分布的宽度
        
        # 生成对数正态分布值
        mu = math.log(mean) - (sigma ** 2) / 2
        value = random.lognormvariate(mu, sigma)
        
        # 限制在范围内
        return max(min_sec, min(max_sec * 1.5, value))  # 允许偶尔超出最大值
    
    else:
        # 默认均匀分布
        return random.uniform(min_sec, max_sec)


def get_contextual_wait_time(
    context: WaitContext,
    config
) -> float:
    """
    根据上下文获取等待时间
    
    Args:
        context: 等待上下文
        config: 配置模块
    
    Returns:
        等待时间（秒）
    """
    distribution = getattr(config, "RANDOM_SLEEP_DISTRIBUTION", "lognormal")
    
    if context == WaitContext.PAGE_LOAD:
        if getattr(config, "CONTEXTUAL_WAIT_ENABLED", False):
            times = getattr(config, "CONTEXTUAL_PAGE_LOAD_SEC", [5.0, 10.0])
            return generate_random_wait(times[0], times[1], distribution)
        return generate_random_wait(
            getattr(config, "RANDOM_SLEEP_MIN_SEC", 5.0),
            getattr(config, "RANDOM_SLEEP_MAX_SEC", 10.0),
            distribution
        )
    
    elif context == WaitContext.USER_INTERACTION:
        if getattr(config, "CONTEXTUAL_WAIT_ENABLED", False):
            times = getattr(config, "CONTEXTUAL_USER_INTERACTION_SEC", [1.0, 3.0])
            return generate_random_wait(times[0], times[1], distribution)
        return generate_random_wait(1.0, 3.0, distribution)
    
    elif context == WaitContext.API_RETRY:
        if getattr(config, "CONTEXTUAL_WAIT_ENABLED", False):
            times = getattr(config, "CONTEXTUAL_API_RETRY_SEC", [0.3, 0.8])
            return generate_random_wait(times[0], times[1], distribution)
        return generate_random_wait(0.3, 0.8, distribution)
    
    elif context == WaitContext.COMMENT_FETCH:
        return generate_random_wait(
            getattr(config, "RANDOM_SLEEP_COMMENTS_MIN_SEC", 3.0),
            getattr(config, "RANDOM_SLEEP_COMMENTS_MAX_SEC", 6.0),
            distribution
        )
    
    else:  # DEFAULT
        return generate_random_wait(
            getattr(config, "RANDOM_SLEEP_MIN_SEC", 5.0),
            getattr(config, "RANDOM_SLEEP_MAX_SEC", 10.0),
            distribution
        )


def get_randomized_batch_n(config) -> int:
    """
    获取随机化的批次暂停阈值
    """
    min_n = getattr(config, "BATCH_PAUSE_EVERY_N_MIN", 8)
    max_n = getattr(config, "BATCH_PAUSE_EVERY_N_MAX", 12)
    return random.randint(min_n, max_n)


class ExponentialBackoff:
    """
    指数退避重试器
    """
    
    def __init__(
        self,
        initial_delay: float = 2.0,
        max_delay: float = 120.0,
        multiplier: float = 2.0,
        max_retries: int = 5,
        jitter: bool = True
    ):
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.max_retries = max_retries
        self.jitter = jitter
        self.current_retry = 0
    
    def reset(self):
        """重置重试计数"""
        self.current_retry = 0
    
    def get_delay(self) -> float:
        """获取当前重试延迟时间"""
        delay = self.initial_delay * (self.multiplier ** self.current_retry)
        delay = min(delay, self.max_delay)
        
        if self.jitter:
            # 添加 ±20% 的抖动
            delay = delay * random.uniform(0.8, 1.2)
        
        return delay
    
    def should_retry(self) -> bool:
        """是否应该继续重试"""
        return self.current_retry < self.max_retries
    
    async def wait_and_increment(self) -> float:
        """等待并增加重试计数"""
        delay = self.get_delay()
        utils.logger.info(f"[ExponentialBackoff] 第 {self.current_retry + 1} 次重试，等待 {delay:.1f}s")
        await asyncio.sleep(delay)
        self.current_retry += 1
        return delay


def is_recoverable_error(status_code: int, config) -> bool:
    """
    判断是否是可恢复的错误
    """
    recoverable = getattr(config, "RECOVERABLE_ERRORS", [408, 429, 500, 502, 503, 504])
    return status_code in recoverable


def is_unrecoverable_error(status_code: int, config) -> bool:
    """
    判断是否是不可恢复的错误
    """
    unrecoverable = getattr(config, "UNRECOVERABLE_ERRORS", [401, 403])
    return status_code in unrecoverable


# ==================== 浏览器行为模拟 ====================

async def simulate_scroll(page, config) -> None:
    """
    模拟页面滚动
    """
    if not getattr(config, "FAKE_ACTION_ENABLE_SCROLL", False):
        return
    
    try:
        # 随机滚动 1-3 次
        scroll_times = random.randint(1, 3)
        for _ in range(scroll_times):
            # 随机滚动距离
            scroll_distance = random.randint(100, 500)
            await page.evaluate(f"window.scrollBy(0, {scroll_distance})")
            await asyncio.sleep(random.uniform(0.3, 0.8))
        
        utils.logger.debug("[AntiCrawl] 模拟滚动完成")
    except Exception as e:
        utils.logger.debug(f"[AntiCrawl] 模拟滚动失败: {e}")


async def simulate_mouse_move(page, config) -> None:
    """
    模拟鼠标移动
    """
    if not getattr(config, "FAKE_ACTION_ENABLE_MOUSE_MOVE", False):
        return
    
    try:
        # 获取页面尺寸
        viewport = page.viewport_size
        if not viewport:
            return
        
        # 随机移动 2-4 次
        move_times = random.randint(2, 4)
        for _ in range(move_times):
            x = random.randint(50, viewport["width"] - 50)
            y = random.randint(50, viewport["height"] - 50)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.1, 0.3))
        
        utils.logger.debug("[AntiCrawl] 模拟鼠标移动完成")
    except Exception as e:
        utils.logger.debug(f"[AntiCrawl] 模拟鼠标移动失败: {e}")


async def simulate_input(page, config) -> None:
    """
    模拟输入框操作（输入后删除）
    """
    if not getattr(config, "FAKE_ACTION_ENABLE_INPUT_SIMULATION", False):
        return
    
    try:
        # 尝试找到搜索框
        search_input = await page.query_selector('input[type="text"], input[type="search"]')
        if search_input:
            # 随机输入 1-3 个字符
            chars = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=random.randint(1, 3)))
            await search_input.type(chars, delay=random.randint(50, 150))
            await asyncio.sleep(random.uniform(0.3, 0.8))
            # 删除输入的字符
            for _ in range(len(chars)):
                await search_input.press("Backspace")
                await asyncio.sleep(random.uniform(0.05, 0.15))
            
            utils.logger.debug("[AntiCrawl] 模拟输入完成")
    except Exception as e:
        utils.logger.debug(f"[AntiCrawl] 模拟输入失败: {e}")


# ==================== 浏览器指纹伪装 ====================

def get_stealth_js() -> str:
    """
    获取用于消除自动化痕迹的 JavaScript 代码
    """
    return """
    // 移除 webdriver 标志
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined
    });
    
    // 伪装 chrome 对象
    window.chrome = {
        runtime: {},
        loadTimes: function() {},
        csi: function() {},
        app: {}
    };
    
    // 修改 permissions API
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
    );
    
    // 伪装 plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    
    // 伪装 languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-CN', 'zh', 'en']
    });
    
    // 轻度修改 Canvas 指纹（添加微小噪声）
    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        if (type === 'image/png' || type === 'image/jpeg') {
            const context = this.getContext('2d');
            if (context) {
                const imageData = context.getImageData(0, 0, this.width, this.height);
                // 添加微小随机噪声
                for (let i = 0; i < imageData.data.length; i += 4) {
                    imageData.data[i] += Math.floor(Math.random() * 2);
                }
                context.putImageData(imageData, 0, 0);
            }
        }
        return originalToDataURL.apply(this, arguments);
    };
    
    // 伪装 WebGL 信息
    const getParameterProxyHandler = {
        apply: function(target, thisArg, args) {
            const param = args[0];
            const gl = thisArg;
            
            // UNMASKED_VENDOR_WEBGL
            if (param === 37445) {
                return 'Intel Inc.';
            }
            // UNMASKED_RENDERER_WEBGL
            if (param === 37446) {
                return 'Intel Iris OpenGL Engine';
            }
            
            return Reflect.apply(target, thisArg, args);
        }
    };
    
    try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        if (gl) {
            WebGLRenderingContext.prototype.getParameter = new Proxy(
                WebGLRenderingContext.prototype.getParameter,
                getParameterProxyHandler
            );
        }
    } catch (e) {}
    
    console.log('[Stealth] Anti-detection scripts loaded');
    """


async def inject_stealth_scripts(page, config) -> None:
    """
    注入反检测脚本到页面
    """
    if not getattr(config, "BROWSER_REMOVE_WEBDRIVER_FLAG", False):
        return
    
    try:
        await page.add_init_script(get_stealth_js())
        utils.logger.debug("[AntiCrawl] 反检测脚本已注入")
    except Exception as e:
        utils.logger.warning(f"[AntiCrawl] 注入反检测脚本失败: {e}")
