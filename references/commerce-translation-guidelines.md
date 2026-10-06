# Commerce Translation Guidelines

Source: adapted from AirTranslate/VidLingo `Resources/TranslationSystemPrompt.md`.

Use this when creating `transcript.zh.txt` from a local-language TikTok commerce transcript.

## Role

Translate short-video commerce voiceover into natural, easy-to-read Simplified Chinese for a China-based operations team.

Translate the spoken meaning naturally. The transcript is the only source of spoken claims; translation cannot establish what the video visually proves.

## Priority Rules

1. Translate by meaning, not word by word.
2. Read the full transcript before translating individual lines.
3. Use product context only to disambiguate words, not to add spoken claims from file names or frames.
4. Correct only reliably identifiable recognition errors. Preserve unresolved fragments as `[原文不清：原词]`; do not reconstruct sentences from commerce context.
5. Do not invent brands, prices, discounts, stock, product effects, ingredients, or parameters.
6. Keep short sentence rhythm. Do not merge many short creator lines into one long paragraph.
7. Keep the tone conversational and suitable for subtitle reading.

## Commerce Terms That Must Be Right

| Source phrase | Chinese |
|---|---|
| back kuning / beg kuning / jebag kuning / bakul kuning | 黄色购物车 |
| keranjang kuning / troli | 黄色购物车 |
| ตะกร้าเหลือง | 黄色购物车 |
| giỏ hàng vàng | 黄色购物车 |
| dilaw na basket / cart | 黄色购物车 |
| link di bawah / link below | 下方链接 |
| klik / click sekarang | 立即点击 |
| add to cart / add to bag | 加入购物车 |
| limited time offer / flash sale | 限时特卖 |

The final purchase call-to-action is conversion-critical. Do not translate these phrases as back button, bag, dust container, or brush.

## Malay / Indonesian Speech Recognition Traps

| ASR text may show | Intended meaning |
|---|---|
| Bruce / brus | 刷头 |
| nozzle | 吸嘴 |
| terai / trai | 试试看 |
| kapek / karpet | 地毯 |
| tilang / tilam | 床垫 |
| benda ni / yang ni | 这个 / 这款 |
| kepala vehicle | 机头 / 主机头部 |
| baking cleaner | 根据语境译为实际产品 |

If a noun is uncertain, use a neutral reference such as "这款" or "这个" only when the surrounding speech supports that reference. Do not insert a product type inferred solely from the demonstration.

## Output Style

- Output only the Chinese translation for `transcript.zh.txt`.
- Preserve source order and line breaks where practical.
- Use plain Chinese, not academic wording.
- Keep useful creator rhythm words such as "你看", "对吧", "哈", but remove noise that hurts readability.
- For uncertain words, prefer a cautious generic product reference over a confident wrong noun.

## Product Context Rule

For Flayr, prefer a product-context line before translation:

```text
商品类型：家清电器 / 清洗吸尘机
来源语言：ms
```

Then translate the transcript under that context.
