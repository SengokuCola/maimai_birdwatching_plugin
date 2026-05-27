# 麦麦观鸟插件

这个插件给麦麦提供四个图片识别工具：

- `recognize_bird`：查询某条消息中的图片是什么鸟。
- `recognize_animal`：查询某条消息中的图片是什么动物。
- `recognize_plant`：查询某条消息中的图片是什么植物。
- `recognize_dish`：查询某条消息中的图片是什么菜品，默认关闭。

插件默认沿用百度智能云「图像识别」接口。鸟类识别可以在配置中切换到 HHo AI 开放平台「懂鸟动物识别 API V2.0」，作为更面向鸟类场景的升级选项。动物、植物和菜品工具仍使用百度智能云接口。

接口文档见：

- HHo 懂鸟接口：https://ai.open.hhodata.com/doc
- 动物识别接口：https://cloud.baidu.com/doc/IMAGERECOGNITION/s/Zk3bcxdfr
- 植物识别接口：https://cloud.baidu.com/doc/IMAGERECOGNITION/s/Mk3bcxe9i
- 菜品识别接口：https://cloud.baidu.com/doc/IMAGERECOGNITION/s/tk3bcxbb0
- 图像识别产品文档：https://cloud.baidu.com/doc/IMAGERECOGNITION/index.html

## 可选：申请 HHo API

HHo love 懂鸟官网：https://ai.open.hhodata.com/
当前使用的接口地址是：`https://ai.open.hhodata.com/api/v2/dongniao`
需要添加客服微信获取api-key

## 申请百度 API

1. 登录百度智能云控制台：https://console.bce.baidu.com/
2. 进入「人工智能」或「AI 开放平台」相关入口，找到「图像识别」服务。
3. 开通需要使用的识别能力：动物识别、植物识别；如果要使用菜品工具，还需要开通菜品识别。
4. 插件当前使用的接口地址是：

   ```text
   https://aip.baidubce.com/rest/2.0/image-classify/v1/animal
   https://aip.baidubce.com/rest/2.0/image-classify/v1/plant
   https://aip.baidubce.com/rest/2.0/image-classify/v2/dish
   ```

5. 在控制台创建应用，获取该应用的 `API Key` 和 `Secret Key`。
6. 插件会使用 `API Key` 和 `Secret Key` 自动获取 `access_token`，不需要手动填写 token。

百度接口限制要点：

- 请求方式：`POST`
- 请求类型：`application/x-www-form-urlencoded`
- 图片参数：`image`
- 图片要求：Base64 编码后不超过 4MB，支持 jpg/png/bmp，最短边至少 15px，最长边最大 4096px。
- 可选参数：`top_num` 控制返回结果数量，`baike_num` 控制百科信息数量。
- 菜品识别还支持 `filter_threshold`，配置项名为 `dish_filter_threshold`。

## 配置插件

编辑本目录下的 `config.toml`：

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[bird]
provider = "baidu" # 使用HHo懂鸟接口改为 hho

[baidu]
api_key = "你的百度 API Key"
secret_key = "你的百度 Secret Key"
top_num = 6
baike_num = 1
dish_filter_threshold = 0.95
timeout_seconds = 15.0

[hho]
api_key = "你的 HHo api_key"
area_code = ""
baike_num = 0
send_annotated_image = true
min_box_area_ratio = 0.0005

[tools]
recognize_bird = true
recognize_animal = true
recognize_plant = true
recognize_dish = false
```

字段说明：

- `[bird].provider`：鸟类识别服务，默认 `baidu`；填 `hho` 可切换到 HHo 懂鸟。
- `[baidu].api_key`：百度智能云应用的 API Key。
- `[baidu].secret_key`：百度智能云应用的 Secret Key。
- `[baidu].top_num`：百度返回候选识别结果数量，默认 6。
- `[baidu].baike_num`：百度返回百科信息数量，默认 1；填 0 表示不请求百科信息。
- `[baidu].dish_filter_threshold`：菜品识别过滤阈值，默认 0.95，越高越严格。
- `[baidu].timeout_seconds`：请求百度接口的超时时间。
- `[hho].api_key`：HHo AI 开放平台的 api_key。
- `[hho].area_code`：可选地区码，留空表示不按地区过滤；例如可填 `CN` 优先按中国地区筛选。
- `[hho].baike_num`：HHo 为前几个候选补充百科信息，默认 `0` 表示不请求百科。
- `[hho].send_annotated_image`：识别成功后是否额外发送一张带目标框和鸟名标注的图片，默认开启；不会替代 Maisaka 正常文本回复。
- `[hho].min_box_area_ratio`：HHo 小目标框面积过滤比例，默认 `0.0005`；填 `0` 表示不过滤。
- `recognize_bird`：是否启用鸟类识别工具，默认开启。
- `recognize_animal`：是否启用动物识别工具，默认开启。
- `recognize_plant`：是否启用植物识别工具，默认开启。
- `recognize_dish`：是否启用菜品识别工具，默认关闭；需要手动改为 `true`。

配置完成后，重载或重启插件运行时。也可以在聊天中使用插件管理命令重载：

```text
/pm plugin reload openai.maimai-birdwatching-plugin
```

如果插件尚未加载，可以使用：

```text
/pm plugin load openai.maimai-birdwatching-plugin
```

## 使用方式

用户在聊天里发送一张动物或鸟类图片后，麦麦可以根据消息 ID 调用工具。

工具参数：

```json
{
  "msg_id": "目标图片消息的消息 ID"
}
```

示例对话：

```text
用户：这张图是什么鸟？
麦麦：调用 recognize_bird，传入这条图片消息的 msg_id。
```

```text
用户：这张图里的动物是什么？
麦麦：调用 recognize_animal，传入这条图片消息的 msg_id。
```

```text
用户：这是什么花？
麦麦：调用 recognize_plant，传入这条图片消息的 msg_id。
```

```text
用户：这盘菜叫什么？
麦麦：只有 recognize_dish = true 时，才调用 recognize_dish。
```

## 返回结果

`recognize_animal` 会返回百度动物识别候选结果，包括名称、置信度、百科简介和百科链接。

`recognize_bird` 默认调用百度动物识别接口，再从候选结果中筛选鸟类名称。返回中会包含 `provider = "baidu"`。

如果把 `[bird].provider` 改为 `hho`，`recognize_bird` 会调用 HHo 懂鸟接口，并返回鸟类候选名称、置信度、英文名、拉丁名、物种 ID、目标序号、目标框和可选百科资料。返回中会包含 `provider = "hho"`。如果一张图里检测到多个目标，HHo 会按目标分别返回候选列表，文本里也会显示“目标 1”“目标 2”等位置提示。插件会过滤过小目标；png 图片会在上传前自动转成 jpg；开启 `[hho].send_annotated_image` 时，会额外发送一张 JPEG 标注图，框出保留下来的目标并标出鸟名。

- 如果识别到鸟类，`is_bird_detected = true`。
- 如果没有明显识别到鸟类，`is_bird_detected = false`，同时返回当前服务的候选结果，方便麦麦继续判断或说明不确定性。

`recognize_plant` 会返回植物识别候选结果，包括名称、置信度、百科简介和百科链接。

`recognize_dish` 会返回菜品候选结果，包括菜名、热量、置信度、百科简介和百科链接。该工具默认关闭，需要在 `[tools]` 中手动开启。

## 常见问题

### 提示尚未配置百度 API Key 或 Secret Key

默认鸟类识别、动物、植物、菜品工具都需要百度配置。检查 `config.toml` 中的 `[baidu].api_key` 和 `[baidu].secret_key` 是否为空，并确认保存后已经重载插件。

### 提示目标消息中没有图片

确认传入的是图片消息的 `msg_id`，不是文字消息或转发消息的外层 ID。QQ 中作为“文件附件”发送的 png/jpg 可能只会进入文件消息链路，插件无法按普通图片读取；请以聊天图片形式发送。

### 提示图片超过限制

百度动物识别接口要求图片 Base64 编码后不超过 4MB。HHo 懂鸟接口要求上传图片不超过 2MB；jpg/jpeg 会直接上传，png 会自动转成 jpg 后上传。请压缩图片后重新发送，或在 `[bird].provider` 中选择适合当前图片格式的服务。

### 菜品识别工具不可用

检查 `config.toml` 中是否已经设置：

```toml
[tools]
recognize_dish = true
```

### 识别结果不是鸟

`recognize_bird` 会根据当前 provider 的结果判断鸟类。如果图片主体不清晰、鸟太小、遮挡严重或画面里有多个动物，可能会返回不准确的候选。
