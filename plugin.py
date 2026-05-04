"""麦麦观鸟插件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import base64
import logging
import re
import time

import aiohttp
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


BAIDU_ANIMAL_ENDPOINT = "https://aip.baidubce.com/rest/2.0/image-classify/v1/animal"
BAIDU_DISH_ENDPOINT = "https://aip.baidubce.com/rest/2.0/image-classify/v2/dish"
BAIDU_PLANT_ENDPOINT = "https://aip.baidubce.com/rest/2.0/image-classify/v1/plant"
BAIDU_TOKEN_ENDPOINT = "https://aip.baidubce.com/oauth/2.0/token"
MAX_DOWNLOAD_IMAGE_BYTES = 4 * 1024 * 1024
TOKEN_EXPIRE_SAFETY_SECONDS = 300

logger = logging.getLogger("plugin.maimai_birdwatching_plugin")


BIRD_KEYWORDS = (
    "鸟",
    "雀",
    "鸦",
    "鹊",
    "鸽",
    "鹦鹉",
    "鹤",
    "鹭",
    "鹰",
    "隼",
    "雕",
    "鸮",
    "猫头鹰",
    "雁",
    "鸭",
    "鹅",
    "天鹅",
    "鹬",
    "鸥",
    "鸡",
    "雉",
    "鸵鸟",
    "企鹅",
    "蜂鸟",
    "啄木鸟",
    "翠鸟",
    "燕",
    "鹃",
    "鸫",
    "鹎",
    "莺",
    "鹡鸰",
    "鹀",
    "鹟",
    "鹈鹕",
    "鹳",
    "鹱",
    "鸻",
)


def _tool_param(name: str, param_type: ToolParamType, description: str, required: bool) -> ToolParameterInfo:
    """构造工具参数声明。"""

    return ToolParameterInfo(name=name, param_type=param_type, description=description, required=required)


def _extract_nested_mapping(payload: Any) -> Dict[str, Any]:
    """从 capability 返回值中剥离常见包装层，取出业务字典。"""

    current = payload
    visited: set[int] = set()
    while isinstance(current, dict):
        current_id = id(current)
        if current_id in visited:
            break
        visited.add(current_id)

        for wrapper_key in ("result", "data"):
            nested_value = current.get(wrapper_key)
            if isinstance(nested_value, dict):
                current = nested_value
                break
        else:
            return current
    return {}


def _guess_image_format_from_name(file_name: str, default: str = "png") -> str:
    """根据文件名猜测图片格式。"""

    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "webp", "gif", "bmp"}:
        return "jpeg" if suffix == "jpg" else suffix
    return default


def _guess_image_format_from_bytes(image_bytes: bytes, default: str = "png") -> str:
    """根据图片文件头猜测图片格式。"""

    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"GIF8"):
        return "gif"
    if image_bytes.startswith(b"BM"):
        return "bmp"
    return default


def _decode_base64_image(raw_base64: str) -> Optional[Tuple[str, str]]:
    """解析普通 Base64 或 data URL 图片内容。"""

    normalized_base64 = raw_base64.strip()
    if not normalized_base64:
        return None

    image_format = "png"
    data_url_match = re.match(
        r"^data:image/(?P<format>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$",
        normalized_base64,
        re.DOTALL,
    )
    if data_url_match is not None:
        image_format = data_url_match.group("format").lower()
        normalized_base64 = data_url_match.group("data").strip()

    try:
        image_bytes = base64.b64decode(normalized_base64, validate=True)
    except Exception:
        return None
    if not image_bytes:
        return None

    return _guess_image_format_from_bytes(image_bytes, image_format), base64.b64encode(image_bytes).decode("utf-8")


def _read_image_file(image_path: Path) -> Optional[Tuple[str, str]]:
    """读取本地图片文件并返回格式与 Base64。"""

    if not image_path.exists() or not image_path.is_file():
        return None
    image_bytes = image_path.read_bytes()
    if not image_bytes or len(image_bytes) > MAX_DOWNLOAD_IMAGE_BYTES:
        return None
    image_format = _guess_image_format_from_bytes(image_bytes, _guess_image_format_from_name(image_path.name))
    return image_format, base64.b64encode(image_bytes).decode("utf-8")


def _resolve_file_url(file_url: str) -> Optional[Path]:
    """将 file:// URL 转换为本地路径。"""

    parsed_url = urlparse(file_url)
    if parsed_url.scheme.lower() != "file":
        return None
    if parsed_url.netloc and parsed_url.path:
        return Path(f"//{parsed_url.netloc}{unquote(parsed_url.path)}")
    return Path(unquote(parsed_url.path))


def _download_image_url(image_url: str) -> Optional[Tuple[str, str]]:
    """下载图片 URL 并返回格式与 Base64。"""

    request = Request(image_url, headers={"User-Agent": "MaiBot-birdwatching-plugin/1.0"})
    with urlopen(request, timeout=10) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith("image/"):
            return None

        image_bytes = response.read(MAX_DOWNLOAD_IMAGE_BYTES + 1)
    if not image_bytes or len(image_bytes) > MAX_DOWNLOAD_IMAGE_BYTES:
        return None

    url_path = unquote(urlparse(image_url).path)
    guessed_format = _guess_image_format_from_name(url_path)
    if content_type.startswith("image/"):
        guessed_format = content_type.split(";", 1)[0].split("/", 1)[1].strip() or guessed_format
    image_format = _guess_image_format_from_bytes(image_bytes, guessed_format)
    return image_format, base64.b64encode(image_bytes).decode("utf-8")


def _is_windows_drive_path(reference: str) -> bool:
    """判断字符串是否像 Windows 盘符路径。"""

    return bool(re.match(r"^[A-Za-z]:[\\/]", reference.strip()))


def _read_image_reference(reference: str) -> Optional[Tuple[str, str]]:
    """从本地路径、file URL、http(s) URL、data URL 或 Base64 引用中读取图片。"""

    normalized_reference = reference.strip()
    if not normalized_reference:
        return None

    decoded_base64 = _decode_base64_image(normalized_reference)
    if decoded_base64 is not None:
        return decoded_base64

    parsed_url = urlparse(normalized_reference)
    normalized_scheme = parsed_url.scheme.lower()
    if not _is_windows_drive_path(normalized_reference):
        if normalized_scheme == "file":
            file_path = _resolve_file_url(normalized_reference)
            return _read_image_file(file_path) if file_path is not None else None
        if normalized_scheme in {"http", "https"}:
            return _download_image_url(normalized_reference)
        if normalized_scheme:
            return None

    try:
        image_path = Path(normalized_reference)
    except OSError:
        return None
    return _read_image_file(image_path)


def _extract_image_hash(component: Dict[str, Any]) -> str:
    """从图片消息段中提取图片 hash。"""

    for key in ("hash", "binary_hash", "image_hash", "file_hash"):
        value = str(component.get(key) or "").strip()
        if value:
            return value
    return ""


def _read_cached_image_by_hash(image_hash: str) -> Optional[Tuple[str, str]]:
    """通过图片 hash 从本地图片缓存数据库读取图片。"""

    if not image_hash:
        return None

    try:
        from sqlmodel import select

        from src.common.database.database import get_db_session
        from src.common.database.database_model import Images, ImageType

        with get_db_session() as db:
            statement = select(Images).filter_by(image_hash=image_hash, image_type=ImageType.IMAGE).limit(1)
            image_record = db.exec(statement).first()
            if image_record is None or image_record.no_file_flag:
                return None
            raw_full_path = str(image_record.full_path or "").strip()

        if not raw_full_path:
            return None
        return _read_image_file(Path(raw_full_path).expanduser().resolve())
    except Exception as exc:
        logger.info("读取图片缓存失败：hash=%s error=%s", image_hash[:12], exc, exc_info=True)
        return None


def _iter_image_reference_values(value: Any) -> List[str]:
    """从图片消息段 data 或附加字段中收集可能的图片引用。"""

    if isinstance(value, dict):
        references: List[str] = []
        for key in ("binary_data_base64", "base64", "data_url", "url", "file", "file_path", "path", "data"):
            references.extend(_iter_image_reference_values(value.get(key)))
        return references

    if isinstance(value, list):
        references = []
        for item in value:
            references.extend(_iter_image_reference_values(item))
        return references

    normalized_value = str(value or "").strip()
    if not normalized_value:
        return []
    if normalized_value.startswith("[") and normalized_value.endswith("]") and not normalized_value.startswith("[CQ:"):
        return []

    references = [normalized_value]
    cq_url_match = re.search(r"(?:url|file|path)=([^,\]]+)", normalized_value)
    if cq_url_match is not None:
        references.append(cq_url_match.group(1).strip())
    return references


def _is_bird_name(name: str) -> bool:
    """粗略判断识别结果名称是否为鸟类。"""

    normalized_name = str(name or "").strip()
    return bool(normalized_name) and any(keyword in normalized_name for keyword in BIRD_KEYWORDS)


def _score_to_text(score: Any) -> str:
    """格式化百度返回的置信分数。"""

    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        return ""
    if numeric_score <= 1:
        return f"{numeric_score * 100:.1f}%"
    return f"{numeric_score:.3g}"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class BaiduImageRecognitionConfig(PluginConfigBase):
    """百度智能云图像识别配置。"""

    __ui_label__ = "百度识别"
    __ui_icon__ = "bird"
    __ui_order__ = 1

    api_key: str = Field(default="", description="百度智能云应用的 API Key")
    secret_key: str = Field(default="", description="百度智能云应用的 Secret Key")
    top_num: int = Field(default=6, ge=1, le=10, description="返回预测得分 Top 结果数")
    baike_num: int = Field(default=1, ge=0, le=5, description="返回百科信息条数，0 表示不请求百科")
    dish_filter_threshold: float = Field(default=0.95, ge=0.0, le=1.0, description="菜品识别过滤阈值，越高越严格")
    timeout_seconds: float = Field(default=15.0, ge=3.0, le=60.0, description="百度接口请求超时时间")


class ToolSwitchConfig(PluginConfigBase):
    """工具启用开关。"""

    __ui_label__ = "工具开关"
    __ui_icon__ = "toggle-left"
    __ui_order__ = 2

    recognize_bird: bool = Field(default=True, description="是否启用鸟类识别工具")
    recognize_animal: bool = Field(default=True, description="是否启用动物识别工具")
    recognize_plant: bool = Field(default=True, description="是否启用植物识别工具")
    recognize_dish: bool = Field(default=False, description="是否启用菜品识别工具")


class BirdwatchingPluginConfig(PluginConfigBase):
    """麦麦观鸟插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    baidu: BaiduImageRecognitionConfig = Field(default_factory=BaiduImageRecognitionConfig)
    tools: ToolSwitchConfig = Field(default_factory=ToolSwitchConfig)


class BirdwatchingPlugin(MaiBotPlugin):
    """麦麦观鸟插件。"""

    config_model = BirdwatchingPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._access_token = ""
        self._access_token_expires_at = 0.0

    async def on_load(self) -> None:
        """插件加载回调。"""

    async def on_unload(self) -> None:
        """插件卸载回调。"""

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """插件配置更新回调。"""

        del scope
        del config_data
        del version
        self._access_token = ""
        self._access_token_expires_at = 0.0

    async def _find_message_by_id(self, stream_id: str, msg_id: str) -> Optional[Dict[str, Any]]:
        """通过 Host 提供的单条消息查询能力按消息 ID 查找目标消息。"""

        normalized_stream_id = stream_id.strip()
        normalized_msg_id = msg_id.strip()
        if not normalized_stream_id or not normalized_msg_id:
            return None

        lookup_result = await self.ctx.call_capability(
            "message.get_by_id",
            message_id=normalized_msg_id,
            chat_id=normalized_stream_id,
        )
        if not isinstance(lookup_result, dict):
            raise RuntimeError("message.get_by_id 返回格式异常。")
        capability_result = _extract_nested_mapping(lookup_result)
        if lookup_result.get("success") is False or capability_result.get("success") is False:
            raise RuntimeError(
                str(capability_result.get("error") or lookup_result.get("error") or "message.get_by_id 查询失败。")
            )

        direct_message = capability_result.get("message")
        if direct_message is None:
            return None
        if not isinstance(direct_message, dict):
            raise RuntimeError("message.get_by_id 返回的 message 字段格式异常。")
        return direct_message

    @staticmethod
    def _extract_image_from_message(message: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """从消息中提取第一张图片。"""

        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return None, None, "目标消息结构不合法，无法读取图片。"

        for component in raw_message:
            if not isinstance(component, dict):
                continue
            if str(component.get("type") or "").strip().lower() != "image":
                continue

            binary_data_base64 = str(component.get("binary_data_base64") or "").strip()
            if binary_data_base64:
                image_result = _decode_base64_image(binary_data_base64)
                if image_result is not None:
                    image_format, image_base64 = image_result
                    return image_format, image_base64, None

            image_result = _read_cached_image_by_hash(_extract_image_hash(component))
            if image_result is not None:
                image_format, image_base64 = image_result
                return image_format, image_base64, None

            references = []
            references.extend(_iter_image_reference_values(component.get("data")))
            references.extend(_iter_image_reference_values(component.get("url")))
            references.extend(_iter_image_reference_values(component.get("file")))
            references.extend(_iter_image_reference_values(component.get("file_path")))
            references.extend(_iter_image_reference_values(component.get("path")))
            references.extend(_iter_image_reference_values(component.get("data_url")))
            for reference in references:
                image_result = _read_image_reference(reference)
                if image_result is not None:
                    image_format, image_base64 = image_result
                    return image_format, image_base64, None

            return None, None, "目标消息里有图片标记，但拿不到可供识别的图片二进制内容。"

        return None, None, "目标消息中没有图片。"

    def _require_baidu_config(self) -> Tuple[str, str]:
        """读取并校验百度鉴权配置。"""

        api_key = self.config.baidu.api_key.strip()
        secret_key = self.config.baidu.secret_key.strip()
        if not api_key or not secret_key:
            raise RuntimeError("尚未配置百度智能云 API Key 或 Secret Key。")
        return api_key, secret_key

    async def _get_access_token(self) -> str:
        """获取并缓存百度 access_token。"""

        now = time.time()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        api_key, secret_key = self._require_baidu_config()
        timeout = aiohttp.ClientTimeout(total=self.config.baidu.timeout_seconds)
        params = {
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(BAIDU_TOKEN_ENDPOINT, params=params, headers=headers, data="") as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"百度鉴权请求失败：HTTP {response.status} {payload}")

        if not isinstance(payload, dict):
            raise RuntimeError("百度鉴权返回格式异常。")
        token = str(payload.get("access_token") or "").strip()
        if not token:
            error = payload.get("error_description") or payload.get("error") or "未返回 access_token"
            raise RuntimeError(f"百度鉴权失败：{error}")

        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        self._access_token = token
        self._access_token_expires_at = now + max(60, expires_in - TOKEN_EXPIRE_SAFETY_SECONDS)
        return token

    async def _recognize_baidu_image(
        self,
        image_base64: str,
        endpoint: str,
        service_name: str,
        *,
        include_top_num: bool = False,
        include_dish_filter_threshold: bool = False,
    ) -> Dict[str, Any]:
        """调用百度图像识别接口。"""

        if len(image_base64.encode("utf-8")) > MAX_DOWNLOAD_IMAGE_BYTES:
            raise RuntimeError(f"图片 Base64 编码后超过 4MB，无法提交给百度{service_name}接口。")

        access_token = await self._get_access_token()
        timeout = aiohttp.ClientTimeout(total=self.config.baidu.timeout_seconds)
        params = {"access_token": access_token}
        data: Dict[str, Any] = {
            "image": image_base64,
        }
        if include_top_num:
            data["top_num"] = self.config.baidu.top_num
        if include_dish_filter_threshold:
            data["filter_threshold"] = self.config.baidu.dish_filter_threshold
        if self.config.baidu.baike_num > 0:
            data["baike_num"] = self.config.baidu.baike_num

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                endpoint,
                params=params,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"百度{service_name}请求失败：HTTP {response.status} {payload}")

        if not isinstance(payload, dict):
            raise RuntimeError(f"百度{service_name}返回格式异常。")
        if payload.get("error_code") is not None:
            error_msg = payload.get("error_msg") or payload.get("error_description") or "未知错误"
            raise RuntimeError(f"百度{service_name}失败：{payload.get('error_code')} {error_msg}")
        return payload

    @staticmethod
    def _serialize_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """规范化百度图像识别结果。"""

        raw_results = payload.get("result")
        if not isinstance(raw_results, list):
            return []

        results: List[Dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            baike_info = item.get("baike_info")
            if not isinstance(baike_info, dict):
                baike_info = {}
            result = {
                "name": str(item.get("name") or "").strip(),
                "score": item.get("score", item.get("probability")),
                "calorie": item.get("calorie"),
                "baike_url": str(baike_info.get("baike_url") or "").strip(),
                "description": str(baike_info.get("description") or "").strip(),
            }
            if result["name"]:
                results.append(result)
        return results

    @staticmethod
    def _format_results(results: List[Dict[str, Any]], title: str) -> str:
        """格式化识别结果。"""

        if not results:
            return "百度接口没有返回可用的识别结果。"

        lines = [title]
        for index, item in enumerate(results, start=1):
            score_text = _score_to_text(item.get("score"))
            suffix = f"（置信度 {score_text}）" if score_text else ""
            lines.append(f"{index}. {item['name']}{suffix}")
            calorie = str(item.get("calorie") or "").strip()
            if calorie:
                lines.append(f"   热量：{calorie}")
            description = str(item.get("description") or "").strip()
            if description:
                lines.append(f"   简介：{description}")
            baike_url = str(item.get("baike_url") or "").strip()
            if baike_url:
                lines.append(f"   百科：{baike_url}")
        return "\n".join(lines)

    async def _recognize_message_image(
        self,
        msg_id: str,
        stream_id: str,
        endpoint: str,
        service_name: str,
        *,
        include_top_num: bool = False,
        include_dish_filter_threshold: bool = False,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """按消息 ID 提取图片并调用百度图像识别。"""

        normalized_msg_id = msg_id.strip()
        normalized_stream_id = stream_id.strip()
        if not normalized_msg_id:
            raise RuntimeError("缺少 msg_id，无法识别图片。")
        if not normalized_stream_id:
            raise RuntimeError("缺少当前会话 stream_id，无法按消息 ID 查询图片。")

        target_message = await self._find_message_by_id(normalized_stream_id, normalized_msg_id)
        if target_message is None:
            raise RuntimeError(f"未找到消息 ID 为 {normalized_msg_id} 的消息。")

        image_format, image_base64, image_error = self._extract_image_from_message(target_message)
        del image_format
        if image_error is not None or not image_base64:
            raise RuntimeError(image_error or "无法读取目标消息中的图片。")

        payload = await self._recognize_baidu_image(
            image_base64,
            endpoint,
            service_name,
            include_top_num=include_top_num,
            include_dish_filter_threshold=include_dish_filter_threshold,
        )
        return payload, self._serialize_results(payload)

    def _ensure_tool_enabled(self, tool_name: str, display_name: str) -> None:
        """检查工具开关。"""

        if not getattr(self.config.tools, tool_name):
            raise RuntimeError(f"{display_name}工具当前未启用，请在插件配置的 [tools] 中打开 {tool_name}。")

    @Tool(
        "recognize_animal",
        description="查询某条消息中的图片是什么动物；当用户询问图片里的动物是什么时使用。",
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "要识别的图片消息 ID", True),
        ],
    )
    async def handle_recognize_animal(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """识别某条消息中的动物。"""

        del kwargs
        try:
            self._ensure_tool_enabled("recognize_animal", "动物识别")
            payload, results = await self._recognize_message_image(
                msg_id,
                stream_id,
                BAIDU_ANIMAL_ENDPOINT,
                "动物识别",
                include_top_num=True,
            )
            return {
                "success": True,
                "content": self._format_results(results, "动物识别结果："),
                "results": results,
                "raw_response": payload,
                "target_message_id": msg_id.strip(),
            }
        except Exception as exc:
            logger.info("recognize_animal 调用失败：msg_id=%s error=%s", msg_id, exc, exc_info=True)
            return {
                "success": False,
                "content": f"动物识别失败：{exc}",
                "target_message_id": msg_id.strip(),
            }

    @Tool(
        "recognize_bird",
        description="查询某条消息中的图片是什么鸟；当用户询问图片里的鸟类品种、鸟名或观鸟识别时使用。",
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "要识别的图片消息 ID", True),
        ],
    )
    async def handle_recognize_bird(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """识别某条消息中的鸟类。"""

        del kwargs
        try:
            self._ensure_tool_enabled("recognize_bird", "鸟类识别")
            payload, results = await self._recognize_message_image(
                msg_id,
                stream_id,
                BAIDU_ANIMAL_ENDPOINT,
                "动物识别",
                include_top_num=True,
            )
            bird_results = [item for item in results if _is_bird_name(str(item.get("name") or ""))]
            if bird_results:
                content = self._format_results(bird_results, "鸟类识别结果：")
            else:
                content = self._format_results(results, "没有明显识别到鸟类，百度动物识别的候选结果：")
            return {
                "success": True,
                "is_bird_detected": bool(bird_results),
                "content": content,
                "results": bird_results,
                "all_results": results,
                "raw_response": payload,
                "target_message_id": msg_id.strip(),
            }
        except Exception as exc:
            logger.info("recognize_bird 调用失败：msg_id=%s error=%s", msg_id, exc, exc_info=True)
            return {
                "success": False,
                "content": f"鸟类识别失败：{exc}",
                "target_message_id": msg_id.strip(),
            }

    @Tool(
        "recognize_plant",
        description="查询某条消息中的图片是什么植物；当用户询问图片里的花、草、树、植物名称或植物品种时使用。",
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "要识别的图片消息 ID", True),
        ],
    )
    async def handle_recognize_plant(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """识别某条消息中的植物。"""

        del kwargs
        try:
            self._ensure_tool_enabled("recognize_plant", "植物识别")
            payload, results = await self._recognize_message_image(
                msg_id,
                stream_id,
                BAIDU_PLANT_ENDPOINT,
                "植物识别",
            )
            return {
                "success": True,
                "content": self._format_results(results, "植物识别结果："),
                "results": results,
                "raw_response": payload,
                "target_message_id": msg_id.strip(),
            }
        except Exception as exc:
            logger.info("recognize_plant 调用失败：msg_id=%s error=%s", msg_id, exc, exc_info=True)
            return {
                "success": False,
                "content": f"植物识别失败：{exc}",
                "target_message_id": msg_id.strip(),
            }

    @Tool(
        "recognize_dish",
        description="查询某条消息中的图片是什么菜品；默认未启用，只有配置打开后才用于识别食物、菜名、餐食和热量。",
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "要识别的图片消息 ID", True),
        ],
    )
    async def handle_recognize_dish(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """识别某条消息中的菜品。"""

        del kwargs
        try:
            self._ensure_tool_enabled("recognize_dish", "菜品识别")
            payload, results = await self._recognize_message_image(
                msg_id,
                stream_id,
                BAIDU_DISH_ENDPOINT,
                "菜品识别",
                include_top_num=True,
                include_dish_filter_threshold=True,
            )
            return {
                "success": True,
                "content": self._format_results(results, "菜品识别结果："),
                "results": results,
                "raw_response": payload,
                "target_message_id": msg_id.strip(),
            }
        except Exception as exc:
            logger.info("recognize_dish 调用失败：msg_id=%s error=%s", msg_id, exc, exc_info=True)
            return {
                "success": False,
                "content": f"菜品识别失败：{exc}",
                "target_message_id": msg_id.strip(),
            }


def create_plugin() -> BirdwatchingPlugin:
    """创建插件实例。"""

    return BirdwatchingPlugin()
