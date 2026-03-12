from mcp.types import CallToolResult
from pydantic import Field
from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic.dataclasses import dataclass
from ..core.constant import category_mapping
from bilibili_api import bangumi
from bilibili_api.bangumi import IndexFilter as IF
from typing import Optional


@dataclass
class BangumiTool(FunctionTool):
    name: str = "get_bangumi"
    description: str = (
        "当用户希望推荐番剧时调用。根据用户的描述获取前 5 条推荐的动漫番剧。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "style": {
                    "type": "string",
                    "description": "番剧的风格。默认为全部。可选值有：原创, 漫画改, 小说改, 游戏改, 特摄, 布袋戏, 热血, 穿越, 奇幻, 战斗, 搞笑, 日常, 科幻, 萌系, 治愈, 校园, 儿童, 泡面, 恋爱, 少女, 魔法, 冒险, 历史, 架空, 机战, 神魔, 声控, 运动, 励志, 音乐, 推理, 社团, 智斗, 催泪, 美食, 偶像, 乙女, 职场",
                },
                "season": {
                    "type": "string",
                    "description": "番剧的季度。默认为全部。可选值有：WINTER, SPRING, SUMMER, AUTUMN。其也分别代表一月番、四月番、七月番、十月番",
                },
                "start_year": {
                    "type": "number",
                    "description": "起始年份。默认为空，即不限制年份。",
                },
                "end_year": {
                    "type": "number",
                    "description": "结束年份。默认为空，即不限制年份。",
                },
            },
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        style: str = "",
        season: str = "",
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> str | CallToolResult:
        if style in category_mapping:
            style = getattr(IF.Style.Anime, category_mapping[style], IF.Style.Anime.ALL)
        else:
            style = IF.Style.Anime.ALL

        if season in ["WINTER", "SPRING", "SUMMER", "AUTUMN"]:
            season = getattr(IF.Season, season, IF.Season.ALL)
        else:
            season = IF.Season.ALL

        filters = bangumi.IndexFilterMeta.Anime(
            area=IF.Area.JAPAN,
            year=IF.make_time_filter(start=start_year, end=end_year, include_end=True),
            season=season,
            style=style,
        )
        index = await bangumi.get_index_info(
            filters=filters, order=IF.Order.SCORE, sort=IF.Sort.DESC, pn=1, ps=5
        )

        result = "推荐的番剧:\n"
        for item in index["list"]:
            result += f"标题: {item['title']}\n"
            result += f"副标题: {item['subTitle']}\n"
            result += f"评分: {item['score']}\n"
            result += f"集数: {item['index_show']}\n"
            result += f"链接: {item['link']}\n"
            result += "\n"
        result += "请分点，贴心地回答。不要输出 markdown 格式。"
        return result
