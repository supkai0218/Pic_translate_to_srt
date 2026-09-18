# openai/gpt-image-2.5-flare/edit

> GPT Image 2.5 Flare Edit applies natural-language instructions to up to 16 reference images, with an optional mask, arbitrary resolutions up to 3840x2160, five quality tiers including xhigh and max, and transparent-background output. Ready-to-use REST inference API, best performance, no coldstarts, affordable pricing.


## Overview

- **Submit endpoint (POST)**: `https://api.atlascloud.ai/api/v1/model/generateImage` — start an async generation; returns a `prediction_id`
- **Poll endpoint (GET)**: `https://api.atlascloud.ai/api/v1/model/prediction/{prediction_id}` — poll this until the prediction finishes
- **Model ID**: `openai/gpt-image-2.5-flare/edit`


## API Information

This model can be used via our HTTP API or more conveniently via our client libraries.
See the input and output schema below, as well as the usage examples.


### Input Schema

The API accepts the following input parameters:

- **`model`** (`string`, _required_):
  Model name.
  - Default: `"openai/gpt-image-2.5-flare/edit"`
  - Options: "openai/gpt-image-2.5-flare/edit"

- **`images`** (`array[string]`, _required_):
  Up to 16 source images to edit. Each entry is a URL, Base64 data, or an asset reference (asset://<ASSET_ID>).
  - Min items: 1
  - Max items: 16

- **`mask`** (`string`, _optional_):
  Optional mask marking the region to edit. A URL, Base64 data, or an asset reference (asset://<ASSET_ID>). Transparent pixels mark the area the model should replace; it must match the first source image's dimensions.

- **`prompt`** (`string`, _required_):
  Text description of the desired image. Up to 32000 characters.
  - Default: `"Replace the background with a sunlit meadow, keep the subject unchanged."`

- **`quality`** (`string`, _optional_):
  Rendering quality tier. gpt-image-2.5 adds xhigh and max on top of low/medium/high. Higher tiers cost more per image.
  - Default: `"medium"`
  - Options: "auto", "low", "medium", "high", "xhigh", "max"

- **`size`** (`string`, _optional_):
  optional string or auto or 1024x1024 or 1536x1024 or 5 moreThe size ofthe generated images.For gpt-image-2.5 is supported as WIDTHxHEIGHT strings,for example 1536x864 . Width and height mustboth be divisible by 16 and the requested aspect ratio must be between 1:3 and 3:1.Resolutions above2560x1440 are experimental, and the maximum supported resolution is 3840x2160 .The requesteosize must also satisfy the model's current pixel and edge limits.The standard sizes 1024x1024.1536x1024 ,and 1024x1536 are supported by the GPT image models; auto is supported for modelsthat allow automatic sizing.For dall-e-2,use oneof 256x256,512x512,or 1024x1024.Fordall-e-3,use oneof 1024x1024,1792x1024,or 1024x1792.
  - Default: `"1024x1024"`
  - Options: "auto", "1024x768", "768x1024", "1024x1536", "1536x1024", "2048x2048", "2048x1152", "1152x2048", "2560x1088", "1088x2560", "2880x2160", "2160x2880", "3840x2160", "2160x3840"

- **`background`** (`string`, _optional_):
  Background of the generated image. When using transparent, set the output format to png or webp.
  - Default: `"auto"`
  - Options: "auto", "opaque", "transparent"

- **`output_format`** (`string`, _optional_):
  Output image format.
  - Default: `"png"`
  - Options: "png", "jpeg", "webp"

- **`n`** (`integer`, _optional_):
  Number of images to generate, 1 to 10. Each image is billed separately.
  - Default: `1`
  - Min: 1
  - Max: 10



**Required Parameters Example**:

```json
{
  "model": "openai/gpt-image-2.5-flare/edit",
  "prompt": "Replace the background with a sunlit meadow, keep the subject unchanged.",
  "images": [
    ""
  ]
}
```


**Full Example**:

```json
{
  "model": "openai/gpt-image-2.5-flare/edit",
  "images": [
    ""
  ],
  "mask": "",
  "prompt": "Replace the background with a sunlit meadow, keep the subject unchanged.",
  "quality": "medium",
  "size": "1024x1024",
  "background": "auto",
  "output_format": "png",
  "n": 1
}
```


### Output Schema

The API returns the following output format:


- **`id`** (`string`, _optional_):
  Unique identifier for the prediction.

- **`urls`** (`object`, _optional_):
  Object containing related API endpoints.

- **`model`** (`string`, _optional_):
  Model ID used for the prediction.

- **`status`** (`string`, _optional_):
  Status of the task: created, processing, completed, or failed.

- **`outputs`** (`array[string]`, _optional_):
  Array of URLs to the generated images. Empty when status is not completed.

- **`created_at`** (`string`, _optional_):
  ISO timestamp of when the request was created.



**Example Response**:

```json
{
  "id": "",
  "urls": {},
  "model": "",
  "status": "",
  "outputs": [
    ""
  ],
  "created_at": ""
}
```


## Usage Examples

### cURL

```bash
# Step 1: Start generation (async)
curl -X POST "https://api.atlascloud.ai/api/v1/model/generateImage" \
  -H "Authorization: Bearer $ATLASCLOUD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "openai/gpt-image-2.5-flare/edit",
  "images": [
    ""
  ],
  "mask": "",
  "prompt": "Replace the background with a sunlit meadow, keep the subject unchanged.",
  "quality": "medium",
  "size": "1024x1024",
  "background": "auto",
  "output_format": "png",
  "n": 1
}'

# Response will contain: {"code": 200, "data": {"id": "prediction_id", "status": "processing"}}

# Step 2: Poll for result (replace {prediction_id} with the id returned above)
curl -X GET "https://api.atlascloud.ai/api/v1/model/prediction/{prediction_id}" \
  -H "Authorization: Bearer $ATLASCLOUD_API_KEY"

# Keep polling until status is "completed", "succeeded" or "failed"
# When completed, outputs will contain the generated content URL(s)
```

## Additional Resources

### Documentation

- [Model Playground](https://www.atlascloud.ai/models/openai/gpt-image-2.5-flare/edit)
