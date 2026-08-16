from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.api import logger, AstrBotConfig
import re

# 公式检测：$$...$$、\[...\]、\(...\)、常见 LaTeX 命令、行内 $...$（内含反斜杠命令或 ^/_ 上下标）
FORMULA_PATTERN = re.compile(
    r"\$\$[\s\S]+?\$\$"
    r"|\\\[[\s\S]+?\\\]"
    r"|\\\([\s\S]+?\\\)"
    r"|\\(?:frac|sum|int|sqrt|prod|lim|begin|alpha|beta|gamma|theta|pi|infty|cdot|times|leq|geq|neq|partial|nabla)\b"
    r"|\$[^$\n]*(?:\\[a-zA-Z]+|[_^]\{)[^$\n]*\$"
)
# 表格检测：Markdown 表格的表头分隔行（如 | --- | :---: |）
TABLE_PATTERN = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", re.MULTILINE)
# 多行代码块检测：```...``` 且内部含换行（单行行内围栏不算，剥离后代码缩进会丢失）
CODE_BLOCK_PATTERN = re.compile(r"```[^\n]*\n[\s\S]*?```")
# HTML 表格检测
HTML_TABLE_PATTERN = re.compile(r"<table[\s>]", re.IGNORECASE)
# 嵌套列表检测：某行是缩进（≥2 空格或 Tab）的列表项，说明存在多级列表，剥离后层级会丢失
NESTED_LIST_PATTERN = re.compile(r"^(?: {2,}|\t+)(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)

T2I_FLAG_KEY = "llm_purifier_force_t2i"

@register("astrbot_plugin_llm_purifier", "monbed", "净化LLM输出：移除思考过程与Markdown标记，复杂格式自动转图片发送", "0.2.0", "https://github.com/monbed/astrbot_plugin_llm_purifier")
class LLMPurifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

    def _cfg(self, key: str, default: bool = True) -> bool:
        v = self.config.get(key)
        return default if v is None else bool(v)

    def _t2i_cfg(self, key: str) -> bool:
        t2i = self.config.get("t2i") or {}
        v = t2i.get(key)
        return True if v is None else bool(v)

    def has_rich_content(self, text: str) -> bool:
        """按配置检测文本中是否含有公式、表格、多行代码块或嵌套列表等剥离后难以阅读的格式"""
        if not self._t2i_cfg("enable"):
            return False
        if self._t2i_cfg("formula") and FORMULA_PATTERN.search(text):
            return True
        if self._t2i_cfg("table") and (TABLE_PATTERN.search(text) or HTML_TABLE_PATTERN.search(text)):
            return True
        if self._t2i_cfg("code_block") and CODE_BLOCK_PATTERN.search(text):
            return True
        if self._t2i_cfg("nested_list") and NESTED_LIST_PATTERN.search(text):
            return True
        return False

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse, *args):
        """
        监听LLM回复，按配置移除思考过程、净化Markdown；
        若含公式/表格等复杂格式，则保留 Markdown 并标记整段转图片发送
        """
        if not resp or not resp.completion_text:
            return

        original_text = resp.completion_text

        # 1. 先移除思考过程（可配置）
        if self._cfg("remove_thinking"):
            no_thinking_text = self.remove_thinking(original_text)
        else:
            no_thinking_text = original_text

        # 2. 含公式/表格时不剥离 Markdown，改走官方文转图（保留排版供渲染）
        if self.has_rich_content(no_thinking_text):
            if original_text != no_thinking_text:
                resp.completion_text = no_thinking_text
            event.set_extra(T2I_FLAG_KEY, True)
            logger.info("[LLM Purifier] 检测到公式/表格等复杂格式，保留 Markdown 并标记文转图发送")
            return

        # 3. 再移除Markdown格式（可配置）
        if self._cfg("remove_markdown"):
            cleaned_text = self.remove_markdown(no_thinking_text)
        else:
            cleaned_text = no_thinking_text
        
        if cleaned_text and original_text != cleaned_text:
            resp.completion_text = cleaned_text
            # 使用 logger 提醒
            original_preview = original_text[:50].replace('\n', '\\n')
            cleaned_preview = cleaned_text[:50].replace('\n', '\\n')
            log_msg = f"\n[LLM Purifier] --------------------------------------------------\n[LLM Purifier] 检测到特定格式并移除:\n[LLM Purifier] 原文: {original_preview}...\n[LLM Purifier] 处理: {cleaned_preview}...\n[LLM Purifier] --------------------------------------------------"
            logger.warning(log_msg)

    @filter.on_decorating_result()
    async def on_decorating(self, event: AstrMessageEvent):
        """
        发送前修饰：若本事件被标记为含公式/表格，
        对结果打 use_t2i 标记，交由框架官方文转图流程（跟随 WebUI 可选的 t2i 模板）整段渲染为图片
        """
        if not event.get_extra(T2I_FLAG_KEY):
            return
        result = event.get_result()
        if result and result.chain:
            result.use_t2i(True)

    def remove_markdown(self, text: str) -> str:
        """
        移除文本中的Markdown格式
        """
        # 移除代码块 (保留内容)
        # 合并处理: 使用 DOTALL 模式匹配 ```...```，非贪婪匹配
        # 尝试移除语言标识符 (如果后面紧跟空白字符)
        text = re.sub(r"```(?:[a-zA-Z0-9+\-]*\s+)?([\s\S]*?)```", r"\1", text)

        # 移除行内代码 `code` -> code
        text = re.sub(r"`([^`]+)`", r"\1", text)
        
        # 移除粗体/斜体 - 优化以避免误伤数学公式
        # Bold: **text** or __text__
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"__([^_]+)__", r"\1", text)
        
        # Italic: *text* or _text_
        # 严格模式: * 前后不能有空格 (CommonMark 标准)，且 * 必须位于词边界或非单词字符旁
        text = re.sub(r"(^|[^\w\*])\*(?!\s)([^*]+)(?<!\s)\*(?=$|[^\w\*])", r"\1\2", text)
        text = re.sub(r"(^|[^\w_])_(?!\s)([^_]+)(?<!\s)_(?=$|[^\w_])", r"\1\2", text)
        
        # 移除标题 (移除 # 但保留文本)
        text = re.sub(r"^(#{1,6})\s+(.*)", r"\2", text, flags=re.MULTILINE)
        
        # 移除引用 (移除 > 但保留文本)
        text = re.sub(r"^>\s+(.*)", r"\1", text, flags=re.MULTILINE)
        
        # 移除链接 [text](url) -> text
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        
        # 移除列表标记 (移除行首的 - 或 *)
        text = re.sub(r"^\s*[-*]\s+(.*)", r"\1", text, flags=re.MULTILINE)
        
        return text

    def remove_thinking(self, text: str) -> str:
        """
        移除文本中的思考过程
        处理逻辑：将文本按空行分段，移除开头那些以英文为主的段落（思考过程），
        直到遇到第一个以中文为主的段落，将其作为正式回复的起点。
        """
        # 1. 移除 <think>...</think> 标签及其内容 (兼容带标准thought标签的模型)
        text = re.sub(r"<think>[\s\S]*?</think>\s*", "", text)
        
        # 2. 针对无标签的样本格式：按连续换行分割为多个段落
        paragraphs = re.split(r'\n(?:\s*\n)+', text)
        start_idx = 0
        
        for i, p in enumerate(paragraphs):
            p_clean = p.strip()
            if not p_clean:
                continue
                
            # 统计这段话里的中英文字符数量
            chinese_chars = len(re.findall(r"[\u4e00-\u9fa5]", p_clean))
            english_chars = len(re.findall(r"[a-zA-Z]", p_clean))
            
            # 判断是否是思考段落：
            # 如果英文字符数量远超中文字符（大于其2倍），则认为是思考段落（对应样本中全英文的分析和标题）
            # 否则，视为已经遇到了正式回复，停止过滤
            if english_chars > 0 and english_chars > (chinese_chars * 2):
                pass # 这个段落属于“全英文/以英文为主”的思考过程，跳过
            else:
                start_idx = i
                break
                
        # 截取从 start_idx 开始的所有段落
        if start_idx < len(paragraphs):
            result = "\n\n".join(paragraphs[start_idx:]).strip()
            # 如果全部过滤完了变空了，回退至原文本
            return result if result else text.strip()
            
        return text.strip()
