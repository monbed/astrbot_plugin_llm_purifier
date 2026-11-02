from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import At, Image, Plain
from astrbot.api import logger, AstrBotConfig
from collections import OrderedDict
import re
import time

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

# 处理范围（参考 astrbot_plugin_meme_manager 的触发范围设计）
SCOPE_CHAT_ONLY = "only_chat_llm"
SCOPE_CHAT_AND_PLUGIN = "chat_and_plugin_llm"
ORIGIN_EXTRA_KEY = "llm_purifier_llm_request_origin"
ORIGIN_CHAT = "chat"
ORIGIN_PLUGIN = "plugin"

# 兼容旧版 AstrBot：没有 on_waiting_llm_request 钩子时不注册来源标记
_on_waiting_llm_request = getattr(filter, "on_waiting_llm_request", None)

# 假 @ 标记：[CQ:at,qq=123]、{{AT:123}}、[at:123]/[at:昵称]，以及裸 @（后续手动解析 QQ 号或昵称）。
# 裸 @ 前不能是字母数字或点，避免把邮箱地址误当成 @。
FAKE_AT_PATTERN = re.compile(
    r"\[CQ:at,qq=(?P<cq>\d+|all)[^\]]*\]"
    r"|\{\{\s*AT\s*[:：]\s*(?P<mk>[^{}\n]+?)\s*\}\}"
    r"|\[\s*at\s*[:：]\s*(?P<br>[^\[\]\n]+?)\s*\]"
    r"|(?<![A-Za-z0-9_.])[@＠]",
    re.IGNORECASE,
)
# 快速判断文本里是否可能有假 @
FAKE_AT_HINT = re.compile(r"[@＠]|\[\s*at\s*[:：]|\{\{\s*AT|\[CQ:at", re.IGNORECASE)
QQ_PATTERN = re.compile(r"\d{5,12}")
# 昵称后跟括号 QQ 号：小明(123456) / 小明（123456）
NAME_WITH_QQ_PATTERN = re.compile(r"[^\s@＠()（）\[\]]{1,32}?\s*[(（]\s*(\d{5,12})\s*[)）]")
# 群成员列表缓存时长（秒）
MEMBER_CACHE_TTL = 600

class LLMPurifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 群昵称 -> QQ 号的短期缓存（来自群聊发言者与消息中的 At），用于解析“@昵称”。
        self._group_name_to_qq: dict[str, OrderedDict[str, str]] = {}
        # 群成员列表缓存：group_id -> (获取时间, {名字: QQ号})
        self._member_cache: dict[str, tuple[float, dict[str, str]]] = {}

    def _scope(self) -> str:
        value = str(self.config.get("scope") or SCOPE_CHAT_ONLY).strip().lower()
        return SCOPE_CHAT_AND_PLUGIN if value == SCOPE_CHAT_AND_PLUGIN else SCOPE_CHAT_ONLY

    def _scope_allows(self, event) -> bool:
        """按配置判断本轮 LLM 回复的来源是否在处理范围内。"""
        try:
            origin = event.get_extra(ORIGIN_EXTRA_KEY)
        except Exception:
            origin = None
        if origin == ORIGIN_PLUGIN:
            return self._scope() == SCOPE_CHAT_AND_PLUGIN
        # 普通聊天；或旧版 AstrBot 无法标记来源时，按普通聊天处理
        return True

    if _on_waiting_llm_request is not None:

        @_on_waiting_llm_request(priority=99999)
        async def mark_llm_request_origin(self, event: AstrMessageEvent):
            """在框架构建默认请求前记录来源：已存在 provider_request 说明是插件调用 event.request_llm。"""
            origin = ORIGIN_PLUGIN if event.get_extra("provider_request") is not None else ORIGIN_CHAT
            event.set_extra(ORIGIN_EXTRA_KEY, origin)

    def _cfg(self, key: str, default: bool = True) -> bool:
        v = self.config.get(key)
        return default if v is None else bool(v)

    def _t2i_cfg(self, key: str, default: bool = True) -> bool:
        t2i = self.config.get("t2i") or {}
        v = t2i.get(key)
        return default if v is None else bool(v)

    def _get_group_cache(self, group_id) -> OrderedDict[str, str]:
        key = str(group_id or "")
        cache = self._group_name_to_qq.get(key)
        if cache is None:
            cache = OrderedDict()
            self._group_name_to_qq[key] = cache
        return cache

    def _remember_name(self, group_id, name, qq) -> None:
        name = str(name or "").strip()
        qq = str(qq or "").strip()
        if not group_id or not name or not qq or qq == "all":
            return
        cache = self._get_group_cache(group_id)
        cache[name] = qq
        cache.move_to_end(name)
        while len(cache) > 200:
            cache.popitem(last=False)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        """记录群里最近出现的昵称和账号（含消息中 At 的对象），供假 @ 解析使用。"""
        group_id = event.get_group_id()
        if not group_id:
            return
        self._remember_name(group_id, event.get_sender_name(), event.get_sender_id())
        try:
            for comp in event.get_messages() or []:
                if isinstance(comp, At):
                    self._remember_name(group_id, getattr(comp, "name", ""), comp.qq)
        except Exception:
            pass

    async def _get_member_names(self, event, group_id) -> dict[str, str]:
        """通过 OneBot 接口获取群成员名片/昵称 -> QQ 号映射（带缓存，仅 aiocqhttp）。"""
        key = str(group_id)
        cached = self._member_cache.get(key)
        if cached and time.time() - cached[0] < MEMBER_CACHE_TTL:
            return cached[1]
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        if api is None or not hasattr(api, "call_action"):
            return {}
        try:
            members = await api.call_action("get_group_member_list", group_id=int(group_id))
        except Exception as e:
            logger.debug(f"[LLM Purifier] 获取群成员列表失败: {e}")
            self._member_cache[key] = (time.time(), cached[1] if cached else {})
            return cached[1] if cached else {}
        mapping: dict[str, str] = {}
        for m in members or []:
            qq = str(m.get("user_id", "") or "")
            if not qq:
                continue
            # 群名片优先，其次 QQ 昵称；同名时不覆盖先出现的
            for name in (m.get("card"), m.get("nickname")):
                name = str(name or "").strip()
                if name and name not in mapping:
                    mapping[name] = qq
        self._member_cache[key] = (time.time(), mapping)
        return mapping

    async def _build_name_map(self, event, need_members: bool) -> dict[str, str]:
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = ""
        if not group_id:
            return {}
        names: dict[str, str] = {}
        if need_members:
            names.update(await self._get_member_names(event, group_id))
        # 最近发言者缓存优先级最高（最贴近模型看到的称呼）
        names.update(self._get_group_cache(group_id))
        return names

    @staticmethod
    def _resolve_target(value: str, names: dict[str, str]) -> str | None:
        """把标记里的值（QQ 号或昵称）解析成 QQ 号。"""
        value = (value or "").strip().lstrip("@＠").strip()
        if not value:
            return None
        m = re.fullmatch(r"(?:.*?[(（]\s*)?(\d{5,12})\s*[)）]?", value)
        if m:
            return m.group(1)
        if value.lower() == "all":
            return None
        return names.get(value)

    @staticmethod
    def _resolve_bare_at(rest: str, names: dict[str, str], sorted_names: list[str]):
        """解析裸 @ 之后的文本，返回 (QQ号, 消耗的字符数)；无法解析返回 (None, 0)。"""
        m = re.match(r"\s?(\d{5,12})(?!\d)", rest)
        if m:
            end = m.end()
            # 吞掉可能跟随的 (昵称) 写法以外的内容不做处理
            return m.group(1), end
        m = NAME_WITH_QQ_PATTERN.match(rest)
        if m:
            return m.group(1), m.end()
        for name in sorted_names:
            if rest.startswith(name):
                end = len(name)
                tail = re.match(r"\s*[(（]\s*\d{5,12}\s*[)）]", rest[end:])
                if tail:
                    end += tail.end()
                return names[name], end
        return None, 0

    def _split_plain(self, text: str, names, sorted_names) -> list:
        """把一段文本拆成 Plain / At 组件列表。"""
        parts: list = []
        buf = ""
        pos = 0
        while True:
            m = FAKE_AT_PATTERN.search(text, pos)
            if not m:
                break
            buf += text[pos : m.start()]
            qq, end = None, m.end()
            if m.group("cq") is not None:
                qq = m.group("cq") if m.group("cq") != "all" else None
            elif m.group("mk") is not None:
                qq = self._resolve_target(m.group("mk"), names)
            elif m.group("br") is not None:
                qq = self._resolve_target(m.group("br"), names)
            else:
                qq, used = self._resolve_bare_at(text[m.end() :], names, sorted_names)
                end = m.end() + used
            if qq:
                if buf:
                    parts.append(Plain(buf))
                    buf = ""
                parts.append(At(qq=qq))
            else:
                buf += text[m.start() : end]
            pos = end
        buf += text[pos:]
        if buf:
            parts.append(Plain(buf))
        return parts

    @staticmethod
    def _tidy_at_spacing(chain: list) -> list:
        """清理 At 前后多余空白，并在 At 后补一个空格，避免文本与 @ 粘连。"""
        out: list = []
        for i, comp in enumerate(chain):
            if isinstance(comp, Plain):
                text = comp.text
                prev = chain[i - 1] if i > 0 else None
                nxt = chain[i + 1] if i + 1 < len(chain) else None
                if isinstance(prev, At):
                    text = text.lstrip(" \t\u200b")
                    # 紧跟换行时保持原样，不在换行前插空格
                    if not text.startswith(("\n", "\r")):
                        text = " " + text
                if isinstance(nxt, At):
                    text = text.rstrip(" \t")
                if text.strip(" \t\u200b") or "\n" in text:
                    out.append(Plain(text))
                continue
            out.append(comp)
            if isinstance(comp, At):
                nxt = chain[i + 1] if i + 1 < len(chain) else None
                if not isinstance(nxt, Plain) or not nxt.text.strip(" \t\u200b"):
                    out.append(Plain(" "))
        # 去掉链首仅含空格的文本段（含换行的保留）
        while (
            out
            and isinstance(out[0], Plain)
            and not out[0].text.strip(" \t\u200b")
        ):
            out.pop(0)
        return out

    async def _process_at(self, event, result) -> None:
        """将 LLM 输出中的假 @（@昵称、@QQ号、[at:xx]、{{AT:xx}}、[CQ:at,qq=xx]）转换成真实 At 组件。"""
        if not result or not result.chain:
            return
        try:
            if not event.get_group_id():
                return
        except Exception:
            return
        texts = [c.text for c in result.chain if isinstance(c, Plain) and c.text]
        if not any(FAKE_AT_HINT.search(t) for t in texts):
            return

        # 只有出现“@ + 非数字”或标记里含非数字值时，才需要拉取群成员列表
        need_members = any(
            re.search(r"[@＠](?!\s?\d{5,12})\S", t)
            or re.search(r"\[\s*at\s*[:：]\s*[^\d\s\]]", t, re.IGNORECASE)
            or re.search(r"\{\{\s*AT\s*[:：]\s*[^\d\s}]", t, re.IGNORECASE)
            for t in texts
        )
        names = await self._build_name_map(event, need_members)
        sorted_names = sorted(names, key=len, reverse=True)

        new_chain: list = []
        changed = False
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text and FAKE_AT_HINT.search(comp.text):
                parts = self._split_plain(comp.text, names, sorted_names)
                if any(isinstance(p, At) for p in parts):
                    changed = True
                new_chain.extend(parts)
            else:
                new_chain.append(comp)

        if changed:
            original = "".join(t for t in texts)
            result.chain = self._tidy_at_spacing(new_chain)
            targets = ", ".join(str(c.qq) for c in result.chain if isinstance(c, At))
            self._log_box(
                "检测到假 @ 并转换为真实 At:",
                [f"原文: {self._preview(original)}", f"At 对象: {targets}"],
            )

    @staticmethod
    def _log_box(title: str, lines: list[str]) -> None:
        """与 Markdown 净化相同的黄色分隔框日志。"""
        sep = "[LLM Purifier] --------------------------------------------------"
        body = "\n".join(f"[LLM Purifier] {line}" for line in lines)
        logger.warning(f"\n{sep}\n[LLM Purifier] {title}\n{body}\n{sep}")

    @staticmethod
    def _preview(text: str, n: int = 50) -> str:
        return text[:n].replace("\n", "\\n") + ("..." if len(text) > n else "")

    def detect_rich_types(self, text: str) -> list[str]:
        """按配置返回命中的复杂格式类别名称。"""
        if not self._t2i_cfg("enable"):
            return []
        hits = []
        if self._t2i_cfg("formula") and FORMULA_PATTERN.search(text):
            hits.append("数学公式")
        if self._t2i_cfg("table") and (TABLE_PATTERN.search(text) or HTML_TABLE_PATTERN.search(text)):
            hits.append("表格")
        if self._t2i_cfg("code_block") and CODE_BLOCK_PATTERN.search(text):
            hits.append("多行代码块")
        if self._t2i_cfg("nested_list", False) and NESTED_LIST_PATTERN.search(text):
            hits.append("多级嵌套列表")
        return hits

    def has_rich_content(self, text: str) -> bool:
        """按配置检测文本中是否含有公式、表格、多行代码块或嵌套列表等剥离后难以阅读的格式"""
        return bool(self.detect_rich_types(text))

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse, *args):
        """
        监听LLM回复，按配置移除思考过程、净化Markdown；
        若含公式/表格等复杂格式，则保留 Markdown 并标记整段转图片发送
        """
        if not resp or not resp.completion_text:
            return
        if not self._scope_allows(event):
            return

        original_text = resp.completion_text

        # 1. 先移除思考过程（可配置，默认关闭）
        if self._cfg("remove_thinking", False):
            no_thinking_text = self.remove_thinking(original_text)
        else:
            no_thinking_text = original_text

        # 2. 含公式/表格时不剥离 Markdown，改走官方文转图（保留排版供渲染）
        rich_types = self.detect_rich_types(no_thinking_text)
        if rich_types:
            if original_text != no_thinking_text:
                resp.completion_text = no_thinking_text
            event.set_extra(T2I_FLAG_KEY, True)
            self._log_box(
                "检测到复杂格式，保留 Markdown 并整段转图发送:",
                [f"类型: {'、'.join(rich_types)}", f"原文: {self._preview(no_thinking_text)}"],
            )
            return

        # 3. 再移除Markdown格式（可配置）
        if self._cfg("remove_markdown"):
            cleaned_text = self.remove_markdown(no_thinking_text)
        else:
            cleaned_text = no_thinking_text
        
        if cleaned_text and original_text != cleaned_text:
            resp.completion_text = cleaned_text
            self._log_box(
                "检测到特定格式并移除:",
                [f"原文: {self._preview(original_text)}", f"处理: {self._preview(cleaned_text)}"],
            )

    def _build_render_options(self) -> dict:
        """按配置组装渲染参数（透传给官方 t2i 渲染端点）"""
        t2i = self.config.get("t2i") or {}
        img_type = t2i.get("image_type") or "png"
        try:
            viewport_width = int(t2i.get("viewport_width") or 1080)
            viewport_height = int(t2i.get("viewport_height") or 900)
        except (TypeError, ValueError):
            viewport_width, viewport_height = 1080, 900
        options = {
            "full_page": True,
            "type": img_type,
            "device_scale_factor_level": t2i.get("scale") or "ultra",
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
        }
        if img_type == "jpeg":
            try:
                options["quality"] = min(100, max(1, int(t2i.get("quality") or 90)))
            except (TypeError, ValueError):
                options["quality"] = 90
        return options

    async def _render_high_quality(self, text: str) -> str | None:
        """取当前生效的官方 t2i 模板，带高清参数渲染"""
        from astrbot.api import html_renderer

        template_name = "base"
        try:
            template_name = (
                self.context.get_config().get("t2i_active_template") or "base"
            )
        except Exception:
            pass
        tmpl_str = await html_renderer.network_strategy.get_template(template_name)
        data = {"text": text, "version": ""}
        try:
            from astrbot.core.config import VERSION
            data["version"] = f"v{VERSION}"
        except Exception:
            pass
        return await self.html_render(
            tmpl_str, data, return_url=True, options=self._build_render_options()
        )

    @filter.on_decorating_result(priority=10)
    async def on_decorating(self, event: AstrMessageEvent):
        """
        发送前修饰：若本事件被标记为含公式/表格，直接调用官方渲染接口
        （跟随 WebUI 可选的 t2i 模板）按配置的清晰度参数渲染，并替换消息链为图片。
        不走框架的 use_t2i 标记，以绕过其 t2i_word_threshold 字数阈值与写死的 quality=40。
        """
        result = event.get_result()
        if not result or not result.chain:
            return
        # 只处理范围内的 LLM 回复；指令、固定文本等普通消息不处理
        if not result.is_llm_result() or not self._scope_allows(event):
            return

        if not event.get_extra(T2I_FLAG_KEY):
            # 仅文本发送时做假 @ 转换；转图后 @ 无意义，不处理
            try:
                await self._process_at(event, result)
            except Exception as e:
                logger.warning(f"[LLM Purifier] 假 @ 转换失败，保留原文: {e}")
            return

        texts = [c.text for c in result.chain if isinstance(c, Plain)]
        text = "\n".join(t for t in texts if t and t.strip())
        if not text:
            return
        url = None
        try:
            url = await self._render_high_quality(text)
        except Exception as e:
            logger.warning(f"[LLM Purifier] 高清渲染失败，回退官方默认渲染: {e}")
        if not url:
            try:
                url = await self.text_to_image(text, return_url=True)
            except Exception as e:
                logger.error(f"[LLM Purifier] 文转图渲染失败，回退为文本发送: {e}")
                return
        if not url:
            return
        # 与框架 ResultDecorateStage 相同的结果组装方式
        img = Image.fromURL(url) if url.startswith("http") else Image.fromFileSystem(url)
        others = [c for c in result.chain if not isinstance(c, Plain)]
        result.chain = [img] + others

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

        # 移除图片 ![alt](url) -> alt (提前于普通链接处理避免残留 "!")
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)

        # 移除链接 [text](url) -> text
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

        # 移除粗体/斜体 - 优化以避免误伤数学公式
        # Bold: **text** or __text__ (非贪婪，支持内部含特殊符号)
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"__(.*?)__", r"\1", text)

        # Italic: *text* or _text_
        # 严格模式: * 前后不能有空格 (CommonMark 标准)，且 * 必须位于词边界或非单词字符旁
        text = re.sub(r"(^|[^\w\*])\*(?!\s)([^*]+)(?<!\s)\*(?=$|[^\w\*])", r"\1\2", text)
        text = re.sub(r"(^|[^\w_])_(?!\s)([^_]+)(?<!\s)_(?=$|[^\w_])", r"\1\2", text)

        # 移除删除线 ~~text~~ -> text
        text = re.sub(r"~~(.*?)~~", r"\1", text)

        # 移除标题 (移除 # 但保留文本)
        text = re.sub(r"^(#{1,6})\s+(.*)", r"\2", text, flags=re.MULTILINE)

        # 移除引用 (处理嵌套情况: >>> text -> text)
        text = re.sub(r"^(?:>\s*)+(.*)", r"\1", text, flags=re.MULTILINE)

        # 移除列表标记 (移除行首的 -, *, +)
        text = re.sub(r"^\s*[-*+]\s+(.*)", r"\1", text, flags=re.MULTILINE)
        
        return text

    def remove_thinking(self, text: str) -> str:
        """
        移除文本中的思考过程
        处理逻辑：将文本按空行分段，移除开头那些以英文为主的段落（思考过程），
        直到遇到第一个以中文为主的段落，将其作为正式回复的起点。
        """
        # 针对无标签的样本格式：按连续换行分割为多个段落
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
