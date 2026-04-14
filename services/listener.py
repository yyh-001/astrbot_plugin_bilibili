import asyncio
import re
import time
import traceback
from collections import OrderedDict, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import AtAll, File, Image, Node, Plain

from ..bili_client import BiliClient
from ..core.constant import BANNER_PATH, LOGO_PATH
from ..core.data_manager import DataManager
from ..core.models import DynamicParseResult, RenderPayload, SubscriptionRecord
from ..core.utils import (
    create_qrcode,
    image_to_base64,
    is_height_valid,
    render_text_to_plain,
)
from .renderer import Renderer

PLAIN_PUSH_ACTIONS = {
    "DYNAMIC_TYPE_AV": "投稿了新视频",
    "DYNAMIC_TYPE_ARTICLE": "发布了新专栏动态",
    "DYNAMIC_TYPE_DRAW": "发布了新图文动态",
    "DYNAMIC_TYPE_FORWARD": "转发了新动态",
    "DYNAMIC_TYPE_WORD": "发布了新动态",
}
VIDEO_BODY_PREFIX = "投稿了新视频"
GROUP_MESSAGE_TYPE = "GroupMessage"
MIN_AT_ALL_REMAINING = 1
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


class DynamicListener:
    """
    负责后台轮询检查B站动态和直播，并推送更新。
    """

    def __init__(
        self,
        context: Context,
        data_manager: DataManager,
        bili_client: BiliClient,
        renderer: Renderer,
        cfg: dict,
    ):
        self.context = context
        self.data_manager = data_manager
        self.bili_client = bili_client
        self.renderer = renderer
        self.interval_secs = max(1, int(cfg.get("interval_secs", 300)))
        self.task_gap_secs = self._parse_float(cfg.get("task_gap_secs"), 20, minimum=0)
        self.rai = cfg.get("rai", True)
        self.node = cfg.get("node", False)
        self.dynamic_limit = cfg.get("dynamic_limit", 5)
        self.render_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.render_cache_limit = int(cfg.get("render_cache_limit", 32))
        self.plain_push_template = (cfg.get("plain_push_template", "") or "").strip()
        self.plain_push_forward_template = (
            cfg.get("plain_push_forward_template", "") or ""
        ).strip()

    async def start(self):
        """启动后台监听循环（按 UID 任务池调度）。"""
        uid_states: Dict[int, float] = {}
        next_dispatch_at = 0.0

        while True:
            try:
                if self.bili_client.credential is None:
                    logger.warning(
                        "Bilibili 凭据未设置，无法获取动态。请使用 /bili_login 登录或在配置中设置 sessdata。"
                    )
                    await asyncio.sleep(self.interval_secs)
                    continue

                uid_targets = self._build_uid_targets()
                current_uids = set(uid_targets.keys())
                now = time.monotonic()

                for uid in list(uid_states):
                    if uid not in current_uids:
                        uid_states.pop(uid, None)

                for uid in current_uids:
                    uid_states.setdefault(uid, now)

                if not current_uids:
                    await asyncio.sleep(2)
                    continue

                due_uids = [uid for uid in current_uids if uid_states[uid] <= now]
                if not due_uids:
                    next_due_at = min(uid_states[uid] for uid in current_uids)
                    wait_secs = min(max(next_due_at - now, 0.2), 2.0)
                    await asyncio.sleep(wait_secs)
                    continue

                if now < next_dispatch_at:
                    wait_secs = min(max(next_dispatch_at - now, 0.2), 2.0)
                    await asyncio.sleep(wait_secs)
                    continue

                run_uid = min(due_uids, key=lambda uid: (uid_states[uid], uid))
                await self._run_uid_task(run_uid, uid_targets.get(run_uid, []))

                finished_at = time.monotonic()
                uid_states[run_uid] = finished_at + self.interval_secs
                next_dispatch_at = finished_at + self.task_gap_secs
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"UID任务池调度异常: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(1)

    @staticmethod
    def _parse_float(value: Any, default: float, minimum: float = 0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return max(parsed, minimum)

    def _build_uid_targets(self) -> Dict[int, List[Tuple[str, SubscriptionRecord]]]:
        """构建 UID -> 订阅目标列表 的映射，用于 UID 级去重请求。"""
        uid_targets: Dict[int, List[Tuple[str, SubscriptionRecord]]] = {}
        all_subs = self.data_manager.get_all_subscriptions()

        for sub_user, sub_list in all_subs.items():
            for sub_data in sub_list or []:
                uid = sub_data.uid
                try:
                    uid_int = int(uid)
                except (TypeError, ValueError):
                    continue

                uid_targets.setdefault(uid_int, []).append((sub_user, sub_data))

        return uid_targets

    async def _run_uid_task(
        self, uid: int, targets: List[Tuple[str, SubscriptionRecord]]
    ) -> None:
        """执行单个 UID 的任务：动态/直播仅请求一次，再按订阅分发。"""
        if not targets:
            return

        try:
            dyn = await self.bili_client.get_latest_dynamics(uid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"拉取 UID={uid} 动态失败: {e}\n{traceback.format_exc()}")
            dyn = None

        should_check_live = any(
            "live" not in sub_data.filter_types for _, sub_data in targets
        )
        live_room = None
        if should_check_live:
            try:
                live_room = await self.bili_client.get_live_info_by_uids([uid])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"拉取 UID={uid} 直播状态失败: {e}\n{traceback.format_exc()}"
                )
                live_room = None

        for sub_user, sub_data in targets:
            try:
                await self._check_single_up(
                    sub_user=sub_user,
                    sub_data=sub_data,
                    dyn=dyn,
                    live_room=live_room,
                    shared_payload=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"处理订阅者 {sub_user} 的 UP主 {sub_data.uid} 时发生未知错误: {e}\n{traceback.format_exc()}"
                )

    async def _check_single_up(
        self,
        sub_user: str,
        sub_data: SubscriptionRecord,
        dyn: Optional[Dict[str, Any]] = None,
        live_room: Optional[Dict[str, Any]] = None,
        shared_payload: bool = False,
    ):
        """检查单个订阅的UP主是否有更新。"""
        uid = int(sub_data.uid)

        # 检查动态更新
        if dyn is None and not shared_payload:
            dyn = await self.bili_client.get_latest_dynamics(uid)
        if dyn:
            result_list = self._parse_and_filter_dynamics(dyn, sub_data)
            sent = 0
            for result in reversed(result_list):
                if result.has_payload():
                    if sent < self.dynamic_limit:
                        sent += 1
                        await self._handle_new_dynamic(
                            sub_user, result.payload, result.dyn_id
                        )
                    if result.dyn_id:
                        await self.data_manager.update_last_dynamic_id(
                            sub_user, uid, result.dyn_id
                        )
                elif result.dyn_id:
                    await self.data_manager.update_last_dynamic_id(
                        sub_user, uid, result.dyn_id
                    )

        # 检查直播状态
        if "live" in sub_data.filter_types:
            return

        if live_room is None and not shared_payload:
            # lives = await self.bili_client.get_live_info(uid)
            live_room = await self.bili_client.get_live_info_by_uids([uid])
        if live_room:
            await self._handle_live_status(sub_user, sub_data, live_room)

    def _build_plain_header(self, payload: Any, nested: bool) -> str:
        render_type = payload.type
        name = payload.name
        if not isinstance(name, str):
            name = ""
        if not isinstance(render_type, str):
            render_type = ""
        display_name = name.strip() or "未知作者"

        action = PLAIN_PUSH_ACTIONS.get(render_type, "发布了新动态")

        subject = "原动态作者" if nested else "UP 主"
        return f"📣 {subject} 「{display_name}」 {action}:"

    def _build_plain_body(self, payload: Any) -> str:
        summary = (payload.summary or "").strip()
        if summary:
            return summary
        plain_text = render_text_to_plain(payload.text)
        if payload.type == "DYNAMIC_TYPE_AV" and plain_text.startswith(
            VIDEO_BODY_PREFIX
        ):
            return plain_text.removeprefix(VIDEO_BODY_PREFIX).strip()
        return plain_text

    def _compose_plain_push(
        self,
        payload: Any,
        render_fail: bool = False,
        nested: bool = False,
    ) -> list:
        """转换为非图片模式下的消息链。"""
        chain = []
        if render_fail and not nested:
            chain.append(Plain("渲染图片失败了 (´;ω;`)\n"))

        lines = list(
            filter(
                None,
                [
                    self._build_plain_header(payload, nested),
                    (f"标题: {payload.title}" if payload.title else ""),
                    self._build_plain_body(payload),
                ],
            )
        )
        if lines:
            chain.append(Plain("\n".join(lines)))

        for pic in filter(None, payload.image_urls):
            chain.append(Image.fromURL(pic))

        # 转发类型的转发部分会进入此分支
        # [TODO] 此处"转发内容:"后的换行需要实现
        forward_data = getattr(payload, "forward", None)
        if forward_data:
            chain.append(Plain("\u200b\n转发内容:"))
            chain.extend(self._compose_plain_push(forward_data, nested=True))

        url = payload.url
        if url and not nested:
            chain.append(Plain(f"\n{url}"))
        return chain

    def _compose_template_push(self, payload: Any, render_fail: bool = False) -> list:
        """使用自定义模板构建非图片模式下的消息链。"""
        chain: list = []
        if render_fail:
            chain.append(Plain("渲染图片失败了 (´;ω;`)\n"))

        text = self._format_payload_template(self.plain_push_template, payload)
        if text is None:
            return self._compose_plain_push(payload, render_fail)
        if text:
            chain.append(Plain(text))

        for pic in filter(None, payload.image_urls):
            chain.append(Image.fromURL(pic))

        forward_data = getattr(payload, "forward", None)
        if forward_data:
            if self.plain_push_forward_template:
                fwd_text = self._format_payload_template(
                    self.plain_push_forward_template,
                    forward_data,
                    with_action=False,
                )
                if fwd_text is None:
                    chain.extend(self._compose_plain_push(forward_data, nested=True))
                else:
                    chain.append(Plain("\u200b\n转发内容:"))
                    if fwd_text:
                        chain.append(Plain(f"\n{fwd_text}"))
                    for pic in filter(None, forward_data.image_urls):
                        chain.append(Image.fromURL(pic))
            else:
                chain.append(Plain("\u200b\n转发内容:"))
                chain.extend(self._compose_plain_push(forward_data, nested=True))

        return chain

    def _format_payload_template(
        self, template: str, payload: Any, *, with_action: bool = True
    ) -> Optional[str]:
        """用 payload 字段格式化模板，返回去除空行后的文本。格式化失败返回 None。"""
        name = payload.name.strip() if isinstance(payload.name, str) else ""
        render_type = payload.type if isinstance(payload.type, str) else ""
        ctx: Dict[str, str] = {
            "name": name or "未知作者",
            "uid": str(getattr(payload, "uid", "") or ""),
            "title": str(payload.title or ""),
            "text": self._build_plain_body(payload),
            "url": str(payload.url or ""),
        }
        if with_action:
            ctx["action"] = PLAIN_PUSH_ACTIONS.get(render_type, "发布了新动态")

        try:
            formatted = template.format_map(defaultdict(str, ctx))
        except Exception as e:
            logger.warning(f"消息模板格式化失败: {e}，回退到默认格式")
            return None

        lines = [line for line in formatted.split("\n") if line.strip()]
        return "\n".join(lines)

    async def _send_dynamic(
        self, sub_user: str, chain_parts: list, send_node: bool = False
    ):
        if self.node or send_node:
            qqNode = Node(
                uin=0,
                name="AstrBot",
                content=chain_parts,
            )
            await self.context.send_message(
                sub_user, MessageEventResult(chain=[qqNode])
            )
        else:
            await self.context.send_message(
                sub_user, MessageEventResult(chain=chain_parts).use_t2i(False)
            )

    def _cache_render(self, dyn_id: Optional[str], chain_parts: list, send_node: bool):
        """缓存渲染结果，避免同一动态在不同会话重复渲染。"""
        if not dyn_id:
            return
        self.render_cache[dyn_id] = {"chain": chain_parts, "send_node": send_node}
        while len(self.render_cache) > self.render_cache_limit:
            self.render_cache.popitem(last=False)

    async def _handle_new_dynamic(
        self,
        sub_user: str,
        payload: Optional[RenderPayload],
        dyn_id: Optional[str] = None,
    ):
        """处理并发送新的动态通知。"""
        if not payload:
            return

        cached = self.render_cache.get(dyn_id) if dyn_id else None
        if cached:
            logger.debug("动态推送命中缓存: dyn_id=%s sub_user=%s", dyn_id, sub_user)
            await self._send_dynamic(sub_user, cached["chain"], cached["send_node"])
            return

        send_node_flag = self.node
        if not self.rai:
            if self.plain_push_template:
                ls = self._compose_template_push(payload)
            else:
                ls = self._compose_plain_push(payload)
            await self._send_dynamic(sub_user, ls, send_node_flag)
            self._cache_render(dyn_id, ls, send_node_flag)
            logger.info("动态推送完成(纯文本): sub_user=%s dyn_id=%s", sub_user, dyn_id)
            return

        img_path = await self.renderer.render_dynamic(payload)
        if img_path:
            url = payload.url
            if is_height_valid(img_path):
                ls = [Image.fromFileSystem(img_path)]
            else:
                timestamp = int(time.time())
                filename = f"bilibili_dynamic_{timestamp}.jpg"
                ls = [File(file=img_path, name=filename)]
            ls.append(Plain(f"\n{url}"))
            await self._send_dynamic(sub_user, ls, send_node_flag)
            self._cache_render(dyn_id, ls, send_node_flag)
            logger.info(
                "动态推送完成(图片): sub_user=%s dyn_id=%s",
                sub_user,
                dyn_id,
            )
            return

        logger.warning(
            "渲染图片失败，降级纯文本推送: sub_user=%s dyn_id=%s", sub_user, dyn_id
        )
        if self.plain_push_template:
            ls = self._compose_template_push(payload, render_fail=True)
        else:
            ls = self._compose_plain_push(payload, render_fail=True)
        await self._send_dynamic(sub_user, ls, send_node=True)
        logger.info("动态推送完成(降级纯文本): sub_user=%s dyn_id=%s", sub_user, dyn_id)

    @staticmethod
    def _extract_group_session(sub_user: str) -> Optional[Tuple[str, str]]:
        try:
            platform_id, message_type, session_id = sub_user.split(":", 2)
        except ValueError:
            return None
        if message_type != GROUP_MESSAGE_TYPE:
            return None
        group_id = session_id.split("_")[-1].strip()
        if not group_id:
            return None
        return platform_id, group_id

    @staticmethod
    def _extract_action_data(action_result: Any) -> Dict[str, Any]:
        if not isinstance(action_result, dict):
            return {}
        payload = action_result.get("data")
        if isinstance(payload, dict):
            return payload
        return action_result

    @staticmethod
    def _prepend_atall(chain_parts: List[Any]) -> List[Any]:
        return [AtAll(), Plain(" ")] + chain_parts

    @staticmethod
    def _parse_live_start_timestamp(live_room: Dict[str, Any]) -> int:
        try:
            live_start_ts = int(live_room.get("live_time", 0) or 0)
        except (TypeError, ValueError):
            return 0
        if live_start_ts <= 0:
            return 0
        return live_start_ts

    @staticmethod
    def _calc_live_duration_seconds(current_ts: int, live_start_ts: int) -> int:
        if current_ts <= 0 or live_start_ts <= 0:
            return 0
        if current_ts <= live_start_ts:
            return 0
        return current_ts - live_start_ts

    @staticmethod
    def _format_live_duration_text(duration_seconds: int) -> str:
        if duration_seconds <= 0:
            return ""

        hours = duration_seconds // SECONDS_PER_HOUR
        minutes = (duration_seconds % SECONDS_PER_HOUR) // SECONDS_PER_MINUTE
        seconds = duration_seconds % SECONDS_PER_MINUTE
        if hours > 0:
            return f"{hours}小时{minutes}分钟{seconds}秒"
        if minutes > 0:
            return f"{minutes}分钟{seconds}秒"
        return f"{seconds}秒"

    @staticmethod
    def _evaluate_live_transition(
        sub_data: SubscriptionRecord, live_room: Dict[str, Any]
    ) -> Tuple[bool, bool, bool]:
        is_live_now = live_room.get("live_status", "") == 1
        is_live_started = is_live_now and not sub_data.is_live
        is_live_ended = (not is_live_now) and sub_data.is_live
        return is_live_now, is_live_started, is_live_ended

    @staticmethod
    def _build_live_payload(live_room: Dict[str, Any], text: str) -> RenderPayload:
        room_id = int(live_room.get("room_id", 0) or 0)
        link = f"https://live.bilibili.com/{room_id}"
        return RenderPayload(
            banner=image_to_base64(BANNER_PATH),
            name="AstrBot",
            avatar=image_to_base64(LOGO_PATH),
            title=str(live_room.get("title", "Unknown") or "Unknown"),
            url=link,
            qrcode=create_qrcode(link),
            image_urls=[str(live_room.get("cover_from_user", "") or "")],
            text=text,
        )

    async def _send_live_payload(
        self, sub_user: str, payload: RenderPayload, with_atall: bool
    ) -> None:
        if not self.rai:
            ls = self._compose_plain_push(payload)
            if with_atall:
                ls = self._prepend_atall(ls)
            await self.context.send_message(
                sub_user, MessageEventResult(chain=ls).use_t2i(False)
            )
            return
        img_path = await self.renderer.render_dynamic(payload)
        if img_path:
            image_chain = [Image.fromFileSystem(img_path), Plain(f"\n{payload.url}")]
            if with_atall:
                image_chain = self._prepend_atall(image_chain)
            await self.context.send_message(
                sub_user, MessageEventResult(chain=image_chain).use_t2i(False)
            )
            return
        ls = self._compose_plain_push(payload, render_fail=True)
        if with_atall:
            ls = self._prepend_atall(ls)
        await self.context.send_message(
            sub_user, MessageEventResult(chain=ls).use_t2i(False)
        )

    async def _should_send_live_atall(self, sub_user: str, enabled: bool) -> bool:
        if not enabled:
            return False

        group_ctx = self._extract_group_session(sub_user)
        if not group_ctx:
            logger.info("live_atall 仅支持群聊会话，当前会话: %s", sub_user)
            return False

        platform_id, group_id = group_ctx
        platform_inst = self.context.get_platform_inst(platform_id)
        if not platform_inst:
            logger.warning("live_atall 失败：找不到平台实例 %s", platform_id)
            return False

        client = platform_inst.get_client()
        if not client or not hasattr(client, "call_action"):
            logger.warning("live_atall 失败：平台 %s 不支持 call_action", platform_id)
            return False

        group_id_param: int | str = int(group_id) if group_id.isdigit() else group_id
        remain_raw = await client.call_action(
            "get_group_at_all_remain", group_id=group_id_param
        )
        remain_data = self._extract_action_data(remain_raw)
        can_at_all = bool(remain_data.get("can_at_all"))
        group_remain = int(remain_data.get("remain_at_all_count_for_group", 0) or 0)
        self_remain_value = remain_data.get(
            "remain_at_all_count_for_self",
            remain_data.get("remain_at_all_count_for_uin", 0),
        )
        self_remain = int(self_remain_value or 0)

        if not can_at_all:
            logger.info("群 %s 当前不允许 @全体成员", group_id)
            return False
        if group_remain < MIN_AT_ALL_REMAINING or self_remain < MIN_AT_ALL_REMAINING:
            logger.info(
                "群 %s @全体次数不足: group=%s, self=%s",
                group_id,
                group_remain,
                self_remain,
            )
            return False
        return True

    async def _handle_live_status(
        self, sub_user: str, sub_data: SubscriptionRecord, live_room: Dict[str, Any]
    ):
        """处理并发送直播状态变更通知。"""
        current_unix_ts = int(time.time())
        is_live_now, is_live_started, is_live_ended = self._evaluate_live_transition(
            sub_data, live_room
        )
        current_live_start_ts = self._parse_live_start_timestamp(live_room)
        if is_live_now and current_live_start_ts > 0:
            sub_data.last_live_start_ts = current_live_start_ts

        user_name = str(live_room.get("uname", "Unknown") or "Unknown")
        text = ""
        if is_live_started:
            if current_live_start_ts > 0:
                sub_data.last_live_start_ts = current_live_start_ts
            text = f"📣 你订阅的UP 「{user_name}」 开播了！"
            await self.data_manager.update_live_status(sub_user, sub_data.uid, True)
        if is_live_ended:
            cached_live_start_ts = int(sub_data.last_live_start_ts or 0)
            live_start_ts = max(current_live_start_ts, cached_live_start_ts)
            live_duration_seconds = self._calc_live_duration_seconds(
                current_unix_ts, live_start_ts
            )
            duration_text = self._format_live_duration_text(live_duration_seconds)
            if duration_text:
                text = (
                    f"📣 你订阅的UP 「{user_name}」 下播了！<br>"
                    f"本场直播时长：{duration_text}"
                )
            else:
                text = f"📣 你订阅的UP 「{user_name}」 下播了！"
            sub_data.last_live_start_ts = 0
            await self.data_manager.update_live_status(sub_user, sub_data.uid, False)
        if text:
            payload = self._build_live_payload(live_room, text)
            with_atall = await self._should_send_live_atall(
                sub_user,
                bool(sub_data.live_atall) and is_live_started,
            )
            await self._send_live_payload(sub_user, payload, with_atall)

    def _get_dynamic_items(self, dyn: Dict[str, Any], data: SubscriptionRecord):
        """获取动态条目列表。"""
        last = data.last
        items = dyn["items"]
        recent_ids = data.recent_ids
        known_ids = {x for x in ([last] + recent_ids) if x}
        new_items = []

        for item in items:
            if "modules" not in item:
                continue
            # 过滤置顶
            if (
                item["modules"].get("module_tag")
                and item["modules"]["module_tag"].get("text") == "置顶"
            ):
                continue

            if item["id_str"] in known_ids:
                break
            new_items.append(item)

        return new_items

    def _match_filter_regex(
        self, text: Optional[str], filter_regex: List[str], log_template: str
    ) -> bool:
        """检测文本是否命中过滤正则"""
        if not text or not filter_regex:
            return False

        for regex_pattern in filter_regex:
            try:
                if re.search(regex_pattern, text):
                    logger.info(log_template.format(regex_pattern=regex_pattern))
                    return True
            except re.error:
                logger.warning(f"无效的正则表达式: {regex_pattern}")
                continue

        return False

    def _parse_and_filter_dynamics(
        self, dyn: Dict[str, Any], data: SubscriptionRecord
    ) -> List[DynamicParseResult]:
        """
        解析并过滤动态。
        """
        filter_types = data.filter_types
        filter_regex = data.filter_regex
        uid = str(data.uid)
        items = self._get_dynamic_items(dyn, data)  # 不含last及置顶的动态列表
        result_list: List[DynamicParseResult] = []
        if not items:
            return result_list

        for item in items:
            dyn_id = item["id_str"]
            item_type = item.get("type")

            if item_type == "DYNAMIC_TYPE_FORWARD":
                result = self._handle_forward_dynamic(
                    item, dyn_id, uid, filter_types, filter_regex
                )
            elif item_type in ("DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"):
                result = self._handle_draw_or_word_dynamic(
                    item, dyn_id, uid, filter_types, filter_regex
                )
            elif item_type == "DYNAMIC_TYPE_AV":
                result = self._handle_video_dynamic(item, dyn_id, uid, filter_types)
            elif item_type == "DYNAMIC_TYPE_ARTICLE":
                result = self._handle_article_dynamic(item, dyn_id, uid, filter_types)
            else:
                # dyn_id记为None，避免未识别类型挤占正常动态缓存
                result = DynamicParseResult.skip(None, "unsupported type")

            result_list.append(result)

        return result_list

    def _handle_forward_dynamic(
        self,
        item: Dict,
        dyn_id: str,
        uid: str,
        filter_types: List[str],
        filter_regex: List[str],
    ) -> DynamicParseResult:
        """处理转发动态的过滤与渲染数据准备。"""
        try:
            is_forward_lottery = (
                item["orig"]["modules"]["module_dynamic"]["major"]["opus"]["summary"][
                    "rich_text_nodes"
                ][0].get("text")
                == "互动抽奖"
            )
        except (KeyError, TypeError):
            is_forward_lottery = False

        if "forward_lottery" in filter_types and is_forward_lottery:
            logger.info(f"转发互动抽奖在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "forward_lottery")

        if "forward" in filter_types:
            logger.info(f"转发类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "forward")

        try:
            content_text = item["modules"]["module_dynamic"]["desc"]["text"]
        except (TypeError, KeyError):
            content_text = ""

        if "lottery" in filter_types and re.search(
            r"恭喜.*等\d+位同学中奖，已私信通知，详情请点击抽奖查看。",
            content_text,
        ):
            logger.info(f"转发内容为抽奖在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "lottery")

        if self._match_filter_regex(
            content_text, filter_regex, "转发内容匹配正则 {regex_pattern}。"
        ):
            return DynamicParseResult.skip(dyn_id, "regex")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        render_data.url = f"https://t.bilibili.com/{dyn_id}"
        render_data.qrcode = create_qrcode(render_data.url)

        render_forward = self.renderer.build_render_data(
            item.get("orig", {}), is_forward=True
        )
        if render_forward.image_urls:
            render_forward.image_urls = [render_forward.image_urls[0]]
        render_data.forward = render_forward.to_forward_payload()
        return DynamicParseResult.deliver(render_data, dyn_id)

    def _handle_draw_or_word_dynamic(
        self,
        item: Dict,
        dyn_id: str,
        uid: str,
        filter_types: List[str],
        filter_regex: List[str],
    ) -> DynamicParseResult:
        """处理图文/文字动态。"""
        if "draw" in filter_types:
            logger.info(f"图文类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "draw")

        major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
        if major.get("type") == "MAJOR_TYPE_BLOCKED":
            logger.info(f"图文动态 {dyn_id} 为充电专属。")
            return DynamicParseResult.skip(dyn_id, "major_blocked")

        opus = major.get("opus", {})
        summary = opus.get("summary", {})
        summary_text = summary.get("text", "")
        rich_nodes = summary.get("rich_text_nodes", [])
        first_node_text = rich_nodes[0].get("text") if rich_nodes else ""

        if first_node_text == "互动抽奖" and "lottery" in filter_types:
            logger.info(f"互动抽奖在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "lottery")

        if self._match_filter_regex(
            summary_text,
            filter_regex,
            f"图文动态 {dyn_id} 的 summary 匹配正则 '{{regex_pattern}}'。",
        ):
            return DynamicParseResult.skip(dyn_id, "regex")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        return DynamicParseResult.deliver(render_data, dyn_id)

    def _handle_video_dynamic(
        self, item: Dict, dyn_id: str, uid: str, filter_types: List[str]
    ) -> DynamicParseResult:
        """处理视频动态。"""
        if "video" in filter_types:
            logger.info(f"视频类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "video")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        return DynamicParseResult.deliver(render_data, dyn_id)

    def _handle_article_dynamic(
        self, item: Dict, dyn_id: str, uid: str, filter_types: List[str]
    ) -> DynamicParseResult:
        """处理专栏文章动态。"""
        if "article" in filter_types:
            logger.info(f"文章类型在过滤列表 {filter_types} 中。")
            return DynamicParseResult.skip(dyn_id, "article")

        major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
        if major.get("type") == "MAJOR_TYPE_BLOCKED":
            logger.info(f"文章 {dyn_id} 为充电专属。")
            return DynamicParseResult.skip(dyn_id, "major_blocked")

        render_data = self.renderer.build_render_data(item)
        render_data.uid = uid
        return DynamicParseResult.deliver(render_data, dyn_id)
