# 麦麦观鸟插件

这个插件给麦麦提供两个图片识别工具：

- `recognize_bird`：查询某条消息中的图片是什么鸟。
- `recognize_animal`：查询某条消息中的图片是什么动物。

插件使用百度智能云「图像识别 / 动物识别」接口。官方接口文档见：

- 动物识别接口：https://cloud.baidu.com/doc/IMAGERECOGNITION/s/Zk3bcxdfr
- 图像识别产品文档：https://cloud.baidu.com/doc/IMAGERECOGNITION/index.html

## 申请百度 API

1. 登录百度智能云控制台：https://console.bce.baidu.com/
2. 进入「人工智能」或「AI 开放平台」相关入口，找到「图像识别」服务。
3. 开通「动物识别」能力。百度文档中的动物识别接口地址是：

   ```text
   https://aip.baidubce.com/rest/2.0/image-classify/v1/animal
   ```

4. 在控制台创建应用，获取该应用的 `API Key` 和 `Secret Key`。
5. 插件会使用 `API Key` 和 `Secret Key` 自动获取 `access_token`，不需要手动填写 token。

百度接口限制要点：

- 请求方式：`POST`
- 请求类型：`application/x-www-form-urlencoded`
- 图片参数：`image`
- 图片要求：Base64 编码后不超过 4MB，支持 jpg/png/bmp，最短边至少 15px，最长边最大 4096px。
- 可选参数：`top_num` 控制返回结果数量，`baike_num` 控制百科信息数量。

## 配置插件

编辑本目录下的 `config.toml`：

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[baidu]
api_key = "你的百度 API Key"
secret_key = "你的百度 Secret Key"
top_num = 6
baike_num = 1
timeout_seconds = 15.0
```

字段说明：

- `api_key`：百度智能云应用的 API Key。
- `secret_key`：百度智能云应用的 Secret Key。
- `top_num`：返回候选识别结果数量，默认 6。
- `baike_num`：返回百科信息数量，默认 1；填 0 表示不请求百科信息。
- `timeout_seconds`：请求百度接口的超时时间。

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

## 返回结果

`recognize_animal` 会返回百度动物识别候选结果，包括名称、置信度、百科简介和百科链接。

`recognize_bird` 会先调用同一个百度动物识别接口，再从候选结果中筛选鸟类名称：

- 如果识别到鸟类，`is_bird_detected = true`。
- 如果没有明显识别到鸟类，`is_bird_detected = false`，同时返回百度动物识别的候选结果，方便麦麦继续判断或说明不确定性。

## 常见问题

### 提示尚未配置百度 API Key 或 Secret Key

检查 `config.toml` 中的 `api_key` 和 `secret_key` 是否为空，并确认保存后已经重载插件。

### 提示目标消息中没有图片

确认传入的是图片消息的 `msg_id`，不是文字消息或转发消息的外层 ID。

### 提示图片超过 4MB

百度动物识别接口要求图片 Base64 编码后不超过 4MB。请压缩图片后重新发送。

### 识别结果不是鸟

`recognize_bird` 基于百度动物识别结果做鸟类筛选。如果图片主体不清晰、鸟太小、遮挡严重或画面里有多个动物，可能会返回非鸟类候选。
