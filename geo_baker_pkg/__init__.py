"""
Geo Baker - 全球地理数据管线 / Global Geospatial Data Pipeline

模块结构 / Module structure:
    core     - 常量、编码、四叉树（基础定义，无外部项目依赖）
    pipeline - 数据下载 + 烘焙编排（DEM/POP/ESA下载、降级链、沿海城市修正）
    io       - 打包 + 查询（GeoPack .dat格式、瓦片查询）
    cli      - 命令行入口
"""
