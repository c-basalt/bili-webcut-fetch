# bili-webcut-fetch
从B站直播间回放剪辑功能下载录播/直播回放（需主播开放权限）

## 安装依赖
- [Python](https://www.python.org/downloads/) 3.8+
    - [Requests](https://requests.readthedocs.io/en/latest/user/install/#install)
    - *[PyCryptodome](https://pycryptodome.readthedocs.io/en/latest/src/installation.html)（可选，如需使用CookieCloud）
- [FFmpeg](https://www.ffmpeg.org/download.html)
    - FFmpeg下载后需添加到PATH，或者放在同一文件夹内

## 使用

输入 `python fetch_vod.py -h` 查看说明，其余说明请查看脚本内注释。注意由于API鉴权限制需要使用账号cookie登录才能使用，可以使用CookieCloud或cookie文件，也可以自己修改实现cookie的传递。使用脚本的一切可能风险由使用者自行负责

## 完整性校验

脚本会对下载的录播文件的视频时长进行检查要求大致符合，对于长度明显不符的会进行报错，但不能完全保证内容完整
