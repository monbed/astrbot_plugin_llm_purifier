from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.api import logger
import re

@register("astrbot_plugin_llm_purifier", "monbed", "移除LLM输出中的思考过程与Markdown格式", "0.0.6", "https://github.com/monbed/astrbot_plugin_llm_purifier")
class ThinkingKillerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
    
    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse, *args):
        """
        监听LLM回复，先移除思考过程，再移除Markdown格式
        """
        if not resp or not resp.completion_text:
            return

        original_text = resp.completion_text
        
        # 1. 先移除思考过程
        no_thinking_text = self.remove_thinking(original_text)
        
        # 2. 再移除Markdown格式
        cleaned_text = self.remove_markdown(no_thinking_text)
        
        if cleaned_text and original_text != cleaned_text:
            resp.completion_text = cleaned_text
            # 使用 logger 提醒
            original_preview = original_text[:50].replace('\n', '\\n')
            cleaned_preview = cleaned_text[:50].replace('\n', '\\n')
            log_msg = f"\n[Thinking & MD Killer] --------------------------------------------------\n[Thinking & MD Killer] 检测到特定格式并移除:\n[Thinking & MD Killer] 原文: {original_preview}...\n[Thinking & MD Killer] 处理: {cleaned_preview}...\n[Thinking & MD Killer] --------------------------------------------------"
            logger.warning(log_msg)

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
