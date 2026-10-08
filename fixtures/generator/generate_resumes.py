# -*- coding: utf-8 -*-
"""ResumeTrace 合成简历生成器:20 份对抗性中文简历(全部虚构,仅测试用,不得当作真实候选人)。

用法: python3 generate_resumes.py
输出: ~/Desktop/简历库/resumes/*.pdf + ~/Desktop/简历库/ground_truth/*.json
对抗性矩阵(对应开发规格第 10/12 节):
  CV01 干净正样本          CV02 跨项目拼接(CV-01)      CV03 技能清单与经历不符(CV-02)
  CV04 提到技术但无日期    CV05 两段重叠任职(CV-03)    CV06 只有年份+"至今"
  CV07 角色措辞"参与"      CV08 不同公司同名项目       CV09 双栏版式
  CV10 扫描图片版(OCR)    CV11 只会 C++(JD-01 OR)     CV12 流批但非 Flink/Spark(示例泛化)
  CV13 总年限长方向年限短  CV14 主导方案设计(JD-04)    CV15 大小写/别名/错字变体
  CV16 信息残缺            CV17 四页密集长简历         CV18 完全无关对照组
  CV19 优先项满足但硬年限不足 CV20 模糊表述无法定位
"""
import hashlib
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (BaseDocTemplate, Frame, HRFlowable, PageTemplate,
                                Paragraph, SimpleDocTemplate, Spacer)

FONT = "STSong-Light"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))

OUT = Path.home() / "Desktop" / "简历库"
RES_DIR = OUT / "resumes"
GT_DIR = OUT / "ground_truth"

# 对锚点 JD(中信期货 应用架构师-风控平台方向 J10034)的六个条件键:
# c_lang        Java 或 C/C++
# c_dev5        5 年以上相关行业软件开发
# c_dir3        3 年以上指定方向(风控/交易平台)经验
# c_stream      掌握流批处理技术(Flink/Spark 为示例)
# p_risk_stream 优先:实时风控或交易类流批项目经验
# p_lead        职责对照:主导技术方案设计(加分展示,不作硬条件)
CANDIDATES = [
    dict(id="CV01", name="陈志远", title="Java 后端架构师", layout="normal",
         phone="138-2201-0101", email="chen.zhiyuan.dev@example.com",
         summary="9 年金融领域软件开发经验,其中 5 年专注于实时风控与交易平台架构设计,主导过两套基于 Flink 的实时风控平台落地。",
         works=[
             dict(org="深睿金融科技(上海)有限公司", role="资深架构师", period="2019.04 - 2025.08",
                  points=["负责公司风控产品线整体技术架构,主导实时风控平台从规则引擎架构向流式架构的演进方案设计与评审。",
                          "搭建风控研发规范与代码评审机制,团队规模 12 人,负责核心模块的技术选型与攻关。"]),
             dict(org="汇通支付技术有限公司", role="高级 Java 开发工程师", period="2016.07 - 2019.03",
                  points=["参与支付清算系统开发,负责交易对账与资金流水模块,使用 Java、Spring、MySQL。",
                          "参与反欺诈风控规则模块的开发与联调,接触实时交易拦截链路。"]),
         ],
         projects=[
             dict(name="实时风控决策平台", role="技术负责人(主导)", period="2021.03 - 2024.06",
                  stack="Java、Apache Flink、Kafka、Redis、HBase、Drools",
                  points=["主导平台技术方案设计:基于 Flink 构建实时交易流特征计算层,支撑毫秒级风控指标聚合。",
                          "设计交易流水与风控规则的流批一体架构,离线特征回补使用 Flink 批模式,日处理交易事件 4 亿条。",
                          "平台上线后欺诈交易拦截率提升至 99.2%,误报率下降 37%。"]),
             dict(name="支付清算对账系统", role="核心开发", period="2017.02 - 2018.12",
                  stack="Java、Spring Boot、MySQL、Quartz",
                  points=["负责多渠道交易对账引擎开发,设计差错处理工作流。"]),
         ],
         skills=["精通 Java,熟悉 JVM 调优与并发编程", "精通 Apache Flink,具备流批一体架构设计经验",
                 "熟悉 Kafka、Redis、HBase 等中间件", "熟悉实时风控、反欺诈业务领域"],
         education=[dict(school="华东理工大学", major="计算机科学与技术", degree="本科", period="2012.09 - 2016.06")],
         traits=["clean_positive"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="SUPPORTED")),

    dict(id="CV02", name="林浩然", title="大数据开发工程师", layout="normal",
         phone="139-2202-0202", email="lin.haoran.bi@example.com",
         summary="7 年软件开发经验,先后从事电商实时计算与信贷风控系统开发。",
         works=[
             dict(org="杭州云集优选科技有限公司", role="大数据开发工程师", period="2020.06 - 2024.11",
                  points=["负责电商实时大屏与推荐特征计算的流式链路开发。"]),
             dict(org="信也信贷信息服务(深圳)有限公司", role="Java 开发工程师", period="2017.07 - 2020.05",
                  points=["从事信贷审批风控系统的后端开发。"]),
         ],
         projects=[
             dict(name="电商实时大屏与推荐特征平台", role="流式开发负责人", period="2021.01 - 2024.06",
                  stack="Apache Flink、Kafka、ClickHouse、Java",
                  points=["基于 Flink 实现商品实时曝光、成交指标的流式聚合,支撑大促大屏秒级刷新。",
                          "构建推荐系统实时特征管道,日处理行为日志 20 亿条。"]),
             dict(name="信贷审批风控系统", role="后端开发", period="2018.03 - 2020.04",
                  stack="Java、Spring Cloud、Drools、MySQL",
                  points=["负责信贷审批规则引擎模块开发,使用 Drools 实现准入与额度规则的配置化管理。",
                          "开发人工审核工作台与审批流水服务,系统为传统同步调用架构,未使用流式计算组件。"]),
         ],
         skills=["熟练使用 Flink 进行实时计算开发", "熟悉 Java、Spring Cloud 微服务开发",
                 "熟悉 Drools 规则引擎", "了解 ClickHouse、Kafka"],
         traits=["cross_scope_stitch:CV-01", "flink_in_project_A_only", "risk_project_no_stream"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="PARTIAL",
                       c_stream="SUPPORTED", p_risk_stream="NOT_EVIDENCED", p_lead="NOT_EVIDENCED"),
         note="对抗点:Flink 只在电商项目、风控项目只有规则引擎;'风控项目使用流批技术'不得 SUPPORTED。"),

    dict(id="CV03", name="王雪", title="数据仓库工程师", layout="normal",
         phone="137-2203-0303", email="wang.xue.dw@example.com",
         summary="6 年数据开发经验,长期从事离线数据仓库建设。",
         works=[
             dict(org="南京数澜信息科技有限公司", role="数据仓库工程师", period="2019.03 - 2025.06",
                  points=["负责公司经营分析数据仓库的分层设计与 ETL 开发。"]),
         ],
         projects=[
             dict(name="集团经营分析离线数仓", role="数仓开发", period="2019.06 - 2025.03",
                  stack="Spark、Hive、HDFS、Scala、SQL",
                  points=["基于 Spark 与 Hive 构建 ODS/DWD/DWS/ADS 四层离线数仓,日调度任务 800 余个。",
                          "负责经营指标口径治理与数据质量监控体系建设。"]),
         ],
         skills=["精通 Spark 离线计算与调优", "熟练使用 Flink 进行实时开发", "精通 Hive SQL",
                 "熟悉数据仓库建模方法论"],
         education=[dict(school="南京邮电大学", major="软件工程", degree="本科", period="2015.09 - 2019.06")],
         traits=["skill_list_vs_experience_mismatch:CV-02", "flink_claimed_spark_only"],
         expected=dict(c_lang="PARTIAL", c_dev5="SUPPORTED", c_dir3="NOT_EVIDENCED",
                       c_stream="SUPPORTED", p_risk_stream="NOT_EVIDENCED", p_lead="NOT_EVIDENCED"),
         note="对抗点:技能清单声称'熟练使用 Flink'但全部项目只有 Spark;'两年 Flink 实时开发'只能 NOT_EVIDENCED/PARTIAL。"),

    dict(id="CV04", name="刘志强", title="实时计算开发工程师", layout="normal",
         phone="136-2204-0404", email="liu.zhiqiang.rt@example.com",
         summary="多年大数据开发经验,擅长实时链路建设。",
         works=[
             dict(org="北京云迹数据技术有限公司", role="大数据开发工程师", period="2018 年入职",
                  points=["长期从事公司实时数据平台建设,主导多个流式计算项目。"]),
         ],
         projects=[
             dict(name="交易流水实时处理平台", role="核心开发", period="",
                  stack="Flink、Kafka、Java",
                  points=["引入 Flink 处理实时交易流水,实现交易异常的秒级监测与告警。",
                          "设计流水数据的 exactly-once 落库方案。"]),
             dict(name="日志实时分析系统", role="开发", period="",
                  stack="Flink、Elasticsearch",
                  points=["基于 Flink 完成应用日志的实时清洗与检索入湖。"]),
         ],
         skills=["熟悉 Flink 实时开发", "熟悉 Java", "了解 Kafka、Elasticsearch"],
         traits=["tech_mentioned_no_dates", "work_period_year_only"],
         expected=dict(c_lang="PARTIAL", c_dev5="NEEDS_REVIEW", c_dir3="NEEDS_REVIEW",
                       c_stream="SUPPORTED", p_risk_stream="PARTIAL", p_lead="NOT_EVIDENCED"),
         note="对抗点:项目均无起止日期,任职只写'2018 年入职';所有年限类条件必须'年限待确认',不得靠推断补齐。"),

    dict(id="CV05", name="赵磊", title="平台架构师", layout="normal",
         phone="135-2205-0505", email="zhao.lei.arch@example.com",
         summary="10 年开发经验,6 年架构设计经验,专注金融交易与风控领域。",
         works=[
             dict(org="上海金橙证券股份有限公司", role="应用架构师", period="2023.06 - 至今",
                  points=["负责风控平台方向的应用架构设计,主导实时风控指标体系的流式改造方案。"]),
             dict(org="杭州安恒信息技术股份有限公司", role="高级开发工程师/架构组长", period="2015.07 - 2023.12",
                  points=["先后负责安全大数据平台与风控数据中台的架构设计,2023 年下半年与新岗位并行交接。"]),
         ],
         projects=[
             dict(name="证券实时风控指标平台", role="架构师(主导)", period="2023.09 - 至今",
                  stack="Java、Flink、Kafka、TiDB",
                  points=["主导技术方案设计,基于 Flink 构建账户级实时风险指标计算,支撑盘中风控拦截。"]),
             dict(name="风控数据中台", role="架构组长", period="2019.05 - 2023.11",
                  stack="Java、Spark、Hive、Spring Cloud",
                  points=["负责中台整体架构,统一风控特征与指标口径,离线链路基于 Spark。"]),
         ],
         skills=["精通 Java", "熟悉 Flink、Spark 流批处理技术", "具备金融风控领域架构设计经验"],
         education=[dict(school="浙江大学", major="计算机应用技术", degree="硕士", period="2012.09 - 2015.06")],
         traits=["overlapping_employment_6m:CV-03", "jinzhi_present"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="SUPPORTED"),
         note="对抗点:2023.06-2023.12 两段任职重叠 6 个月(简历自述交接并行);方向年限计算必须去重,'至今'按搜索执行日。"),

    dict(id="CV06", name="孙晓雨", title="风控研发工程师", layout="normal",
         phone="188-2206-0606", email="sun.xiaoyu.risk@example.com",
         summary="金融风控系统研发经验,熟悉实时计算。",
         works=[
             dict(org="深圳市融安网络科技有限公司", role="风控研发工程师", period="2018 - 至今",
                  points=["负责公司实时风控系统研发,参与流式指标计算模块。"]),
             dict(org="广州银联数据服务有限公司", role="Java 开发工程师", period="2015 - 2018",
                  points=["从事银行卡交易系统后端开发。"]),
         ],
         projects=[
             dict(name="实时风控指标计算模块", role="开发", period="2019 - 2023",
                  stack="Flink、Java、Redis",
                  points=["基于 Flink 实现用户交易行为的实时指标聚合,支撑风控规则引擎决策。"]),
         ],
         skills=["熟悉 Java", "熟悉 Flink 实时开发", "了解风控业务流程"],
         traits=["year_only_no_months", "jinzhi_present"],
         expected=dict(c_lang="SUPPORTED", c_dev5="NEEDS_REVIEW", c_dir3="NEEDS_REVIEW",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="NOT_EVIDENCED"),
         note="对抗点:全部日期只有年份没有月份;'5 年/3 年'年限判断精度不足,应输出年限待确认而非四舍五入硬判。"),

    dict(id="CV07", name="周凯", title="Java 开发工程师", layout="normal",
         phone="186-2207-0707", email="zhou.kai.java@example.com",
         summary="6 年 Java 开发经验,参与过实时风控平台项目。",
         works=[
             dict(org="武汉众邦数据服务有限公司", role="Java 开发工程师", period="2019.05 - 2025.07",
                  points=["在公司风控技术部从事实时风控平台相关的开发与维护工作。"]),
         ],
         projects=[
             dict(name="实时风控平台", role="开发成员(参与)", period="2020.03 - 2024.12",
                  stack="Java、Flink、Kafka、MySQL",
                  points=["参与实时风控平台开发,负责数据接入模块:交易消息的 Kafka 接入与格式校验。",
                          "参与 Flink 作业的日常运维与故障处理,配合架构师完成指标口径变更的编码实现。"]),
         ],
         skills=["熟悉 Java 开发", "了解 Flink 基本使用", "熟悉 MySQL、Kafka"],
         education=[dict(school="武汉理工大学", major="信息管理与信息系统", degree="本科", period="2015.09 - 2019.06")],
         traits=["role_participate_not_lead", "maintenance_level_involvement"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="PARTIAL",
                       c_stream="PARTIAL", p_risk_stream="PARTIAL", p_lead="NOT_EVIDENCED"),
         note="对抗点:全部措辞为'参与/配合/运维',无设计与主导;'主导方案设计'类条件不得 SUPPORTED,证据引用必须区分角色动词。"),

    dict(id="CV08", name="吴婧", title="大数据架构师", layout="normal",
         phone="158-2208-0808", email="wu.jing.bigdata@example.com",
         summary="9 年开发经验,在两家公司先后建设过'实时风控平台'。",
         works=[
             dict(org="成都天府信用管理有限公司", role="大数据架构师", period="2021.02 - 至今",
                  points=["负责公司实时风控平台的架构升级。"]),
             dict(org="北京中关村数科技术有限公司", role="大数据开发工程师", period="2016.07 - 2021.01",
                  points=["从事风控数据平台开发。"]),
         ],
         projects=[
             dict(name="实时风控平台(二期)", role="架构师", period="2021.06 - 2024.12",
                  stack="Java、Flink、Kafka、Doris",
                  points=["主导实时风控平台二期架构设计,将一期 Storm 链路整体迁移至 Flink,实现流批一体的指标加工。"]),
             dict(name="实时风控平台(一期)", role="核心开发", period="2018.04 - 2020.11",
                  stack="Java、Storm、Kafka、HBase",
                  points=["基于 Storm 开发交易实时指标计算拓扑,负责反欺诈场景的流式规则执行。"]),
         ],
         skills=["精通 Java", "熟悉 Flink,了解 Storm", "具备风控平台架构经验"],
         education=[dict(school="电子科技大学", major="计算机科学与技术", degree="本科", period="2012.09 - 2016.06")],
         traits=["same_project_name_two_companies", "scope_attribution"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="SUPPORTED"),
         note="对抗点:两个同名'实时风控平台'项目分属不同公司不同技术栈(Storm/Flink);归属不得混淆,时间线不得跨项目拼接。"),

    dict(id="CV09", name="郑爽", title="风控技术专家", layout="two_col",
         phone="150-2209-0909", email="zheng.shuang.risk@example.com",
         summary="8 年金融 IT 经验,专注实时风控与交易监控方向。",
         works=[
             dict(org="平安普惠信息服务有限公司", role="风控技术专家", period="2020.01 - 至今",
                  points=["负责零售信贷实时风控体系的技术规划与落地。",
                          "主导反欺诈流式特征平台建设,基于 Flink 实现申请行为的实时聚合识别。"]),
             dict(org="上海银行信息技术部", role="高级开发工程师", period="2017.03 - 2019.12",
                  points=["从事信用卡交易系统开发,负责交易授权链路的核心模块。"]),
         ],
         projects=[
             dict(name="反欺诈流式特征平台", role="技术负责人", period="2021.04 - 至今",
                  stack="Flink、Java、Kafka、Redis、Hive",
                  points=["主导平台方案设计,构建申请反欺诈实时特征库,规则命中响应时间小于 100ms。",
                          "离线特征回溯使用 Flink 批处理模式,保证流批口径一致。"]),
             dict(name="信用卡交易授权系统改造", role="核心开发", period="2018.01 - 2019.10",
                  stack="Java、C++、IBM MQ、Oracle",
                  points=["参与授权引擎从集中式向分布式改造,负责 Java 与 C++ 混合链路的接口层开发。"]),
         ],
         skills=["精通 Java,熟悉 C++", "精通 Flink 流批一体开发", "熟悉实时风控/反欺诈领域",
                 "具备技术方案主导经验"],
         education=[dict(school="上海财经大学", major="金融工程", degree="硕士", period="2014.09 - 2017.01")],
         traits=["two_column_layout", "clean_positive"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="SUPPORTED"),
         note="对抗点:内容合格但双栏排版;考验 PDF 文本行序与坐标映射,双栏不得导致跨栏错读。"),

    dict(id="CV10", name="贺平", title="C++ 交易系统开发", layout="scan",
         phone="133-2210-1010", email="he.ping.cpp@example.com",
         summary="8 年 C++ 开发经验,长期从事证券交易与实时风控系统。",
         works=[
             dict(org="华泰证券股份有限公司", role="高级开发工程师", period="2018.05 - 至今",
                  points=["负责集中交易系统风控模块的开发与优化。"]),
             dict(org="杭州恒生电子股份有限公司", role="C++ 开发工程师", period="2017.07 - 2018.04",
                  points=["从事证券交易柜台系统开发。"]),
         ],
         projects=[
             dict(name="交易系统实时风控模块", role="核心开发", period="2019.02 - 2024.08",
                  stack="C++、Kafka、Flink、Redis",
                  points=["负责盘中实时风控检查模块:委托报单的事前风控指标校验,单笔检查延迟低于 1ms。",
                          "与大数据团队协作对接 Flink 实时指标流,负责 C++ 侧指标订阅与本地缓存设计。"]),
         ],
         skills=["精通 C++", "熟悉 Linux 高性能编程", "了解 Flink 与实时风控链路"],
         education=[dict(school="西安交通大学", major="计算机科学与技术", degree="本科", period="2013.09 - 2017.06")],
         traits=["scanned_image_pdf_ocr", "cpp_primary"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="PARTIAL", p_risk_stream="SUPPORTED", p_lead="NOT_EVIDENCED"),
         note="对抗点:整份简历为扫描图片版,必须走 OCR;报告需标注 OCR 来源,定位坐标以 OCR 输出为准。"),

    dict(id="CV11", name="高远", title="量化系统开发工程师", layout="normal",
         phone="189-2211-1111", email="gao.yuan.quant@example.com",
         summary="7 年 C++ 开发经验,专注量化交易与低延迟系统,未使用过 Java。",
         works=[
             dict(org="宁波幻方量化投资管理有限公司", role="量化开发工程师", period="2019.08 - 至今",
                  points=["负责量化交易系统核心链路的开发与性能优化。"]),
             dict(org="深圳迅投科技有限公司", role="C++ 开发工程师", period="2018.07 - 2019.07",
                  points=["从事行情转发与风控前置系统开发。"]),
         ],
         projects=[
             dict(name="低延迟量化交易平台", role="核心开发", period="2020.01 - 至今",
                  stack="C++、DPDK、Kafka、ClickHouse",
                  points=["负责交易执行网关开发,内部穿透延迟优化至微秒级。",
                          "实现事前风控检查链路:仓位、集中度、交易所合规指标的实时校验。"]),
             dict(name="实时行情风控前置", role="开发", period="2018.09 - 2019.06",
                  stack="C++、ZeroMQ、Linux",
                  points=["负责行情驱动的实时风控指标计算模块。"]),
         ],
         skills=["精通 C++ 与 STL", "熟悉 Linux 内核旁路网络编程", "了解量化交易业务流程"],
         education=[dict(school="中国科学技术大学", major="软件工程", degree="硕士", period="2015.09 - 2018.06")],
         traits=["cpp_only_no_java:JD-01", "no_stream_framework"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="NOT_EVIDENCED", p_risk_stream="PARTIAL", p_lead="NOT_EVIDENCED"),
         note="对抗点:JD 中 Java 与 C/C++ 为 OR;只会 C++ 不得因'不会 Java'被否决。c_lang 必须 SUPPORTED。"),

    dict(id="CV12", name="罗佳", title="实时计算工程师", layout="normal",
         phone="151-2212-1212", email="luo.jia.stream@example.com",
         summary="6 年实时数据处理经验,擅长流式架构,未使用过 Flink 与 Spark。",
         works=[
             dict(org="北京字节跳动网络技术有限公司", role="实时计算工程师", period="2020.04 - 至今",
                  points=["负责公司业务风控方向的实时数据链路建设。"]),
             dict(org="小米科技有限责任公司", role="后端开发工程师", period="2019.07 - 2020.03",
                  points=["从事服务端消息系统开发。"]),
         ],
         projects=[
             dict(name="业务风控实时指标引擎", role="核心开发", period="2020.08 - 至今",
                  stack="Kafka Streams、自研流式引擎、Java、Redis",
                  points=["基于 Kafka Streams 与部门自研流式引擎构建风控实时指标计算链路,支撑设备指纹与行为异常的秒级识别。",
                          "负责流式引擎的窗口聚合与状态管理模块,实现批式回补工具保证指标可重算。"]),
         ],
         skills=["精通流式计算原理与状态管理", "熟练使用 Kafka Streams", "熟悉 Java",
                 "具备流批一体的指标口径治理经验"],
         education=[dict(school="北京邮电大学", major="通信工程", degree="本科", period="2015.09 - 2019.06")],
         traits=["stream_batch_without_flink_spark:JD-02", "self_built_engine"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="NOT_EVIDENCED"),
         note="对抗点:Flink/Spark 只是流批处理的示例;Kafka Streams+自研引擎的流批经验应能 SUPPORTED '流批处理能力',不得因未出现 Flink 关键词而否决。"),

    dict(id="CV13", name="梁成", title="Java 后端开发工程师", layout="normal",
         phone="138-2213-1313", email="liang.cheng.be@example.com",
         summary="9 年软件开发经验,近期转向风控平台方向。",
         works=[
             dict(org="广州三七互娱网络科技有限公司", role="Java 开发工程师", period="2016.07 - 2023.10",
                  points=["长期从事游戏运营后台与内容管理系统的 CRUD 开发,负责活动配置、结算报表等模块。"]),
             dict(org="深圳前海微众信科技术有限公司", role="Java 开发工程师", period="2023.11 - 至今",
                  points=["转入风控技术部,参与企业信贷风控平台的功能开发。"]),
         ],
         projects=[
             dict(name="游戏运营管理后台", role="开发", period="2017.01 - 2023.09",
                  stack="Java、Spring、MySQL、Vue",
                  points=["负责运营活动配置、玩家结算与报表模块的迭代开发。"]),
             dict(name="企业信贷风控平台", role="开发", period="2024.01 - 至今",
                  stack="Java、Spring Cloud、Drools、Kafka",
                  points=["负责风控平台审批流与规则配置模块的功能开发,接口层对接外部征信数据源。"]),
         ],
         skills=["熟悉 Java 与 Spring 全家桶", "熟悉 MySQL 设计与优化", "了解风控业务流程"],
         education=[dict(school="广东工业大学", major="网络工程", degree="本科", period="2012.09 - 2016.06")],
         traits=["total_years_long_direction_short", "no_stream_processing"],
         expected=dict(c_lang="SUPPORTED", c_dev5="NOT_EVIDENCED", c_dir3="NOT_EVIDENCED",
                       c_stream="NOT_EVIDENCED", p_risk_stream="NOT_EVIDENCED", p_lead="NOT_EVIDENCED"),
         note="对抗点:总开发年限 9 年但风控方向仅约 1.8 年;总年限不得代替方向年限,c_dir3 不得 SUPPORTED。"),

    dict(id="CV14", name="宋佳", title="解决方案架构师", layout="normal",
         phone="186-2214-1414", email="song.jia.sa@example.com",
         summary="8 年金融 IT 经验,4 年技术方案主导经验,方向为交易与风控平台。",
         works=[
             dict(org="兴业数金融服务(上海)股份有限公司", role="解决方案架构师", period="2021.03 - 至今",
                  points=["主导多个风控与交易类项目的技术方案设计、选型与评审。"]),
             dict(org="福建新大陆软件工程有限公司", role="高级开发工程师", period="2017.07 - 2021.02",
                  points=["从事银行渠道类系统开发。"]),
         ],
         projects=[
             dict(name="集团统一风控中台", role="方案架构师(主导)", period="2021.09 - 2024.12",
                  stack="Java、Flink、Spring Cloud、TiDB",
                  points=["主导整体技术方案设计:统一指标体系、流式特征加工与规则决策分层的架构蓝图与分期路线图。",
                          "基于 Flink 设计实时指标加工层,组织跨团队技术评审并推动落地。"]),
             dict(name="交易反洗钱监测平台", role="方案负责人", period="2022.06 - 2023.12",
                  stack="Java、Spark、Kafka",
                  points=["主导平台技术方案设计,离线可疑交易模型基于 Spark 批处理,实时大额监测走流式链路。"]),
         ],
         skills=["具备技术方案主导与架构评审经验", "熟悉 Java、Flink、Spark",
                 "熟悉风控、反洗钱业务领域"],
         education=[dict(school="福州大学", major="计算机科学", degree="本科", period="2013.09 - 2017.06")],
         traits=["lead_design_positive:JD-04", "clean_positive"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="SUPPORTED"),
         note="对照点:'主导技术方案设计'在 JD 中是岗位职责;本样本用于验证该经历能作为加分展示,但解析器不得为其他候选人生成'曾主导风控项目'硬条件。"),

    dict(id="CV15", name="唐通", title="数据开发工程师", layout="normal",
         phone="177-2215-1515", email="tang.tong.data@example.com",
         summary="5 年数据开发经验,熟悉实时计算与风控数据加工。",
         works=[
             dict(org="上海即科金融信息服务有限公司", role="数据开发工程师", period="2020.08 - 至今",
                  points=["负责风控数据链路的实时与离线开发。"]),
         ],
         projects=[
             dict(name="实肘风控数据平台", role="开发", period="2021.03 - 2025.02",
                  stack="flink、kafka、java、hive",
                  points=["使用 flink(Apache Flink,又称流式计算框架)完成交易数据的实时清洗与指标加工。",
                          "负责实时计算作业的上线与调优,处理消费信贷申请流水。"]),
         ],
         skills=["熟悉 flink 实时开发", "熟悉 java", "了解批处理与流批一体概念"],
         education=[dict(school="上海理工大学", major="计算机技术", degree="硕士", period="2017.09 - 2020.06")],
         traits=["lowercase_tech_names", "alias_variants", "typo_实肘风控", "project_name_typo"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="NOT_EVIDENCED"),
         note="对抗点:技术名小写(flink/java)、别名括注、项目名错字('实肘风控'应为'实时风控');关键词精确匹配不得因此漏召回,语义判断需容错。"),

    dict(id="CV16", name="许岚", title="后端开发工程师", layout="normal",
         phone="155-2216-1616", email="xu.lan.dev@example.com",
         summary="软件开发工程师,有风控相关项目经历。",
         works=[
             dict(org="某互联网金融服务公司", role="后端开发工程师", period="2019 - 2024",
                  points=["从事风控相关系统开发,负责部分服务端模块。"]),
         ],
         projects=[
             dict(name="风控规则系统", role="开发", period="",
                  stack="Java、Spring",
                  points=["负责风控规则配置接口的开发与维护。"]),
         ],
         skills=["熟悉 Java", "了解风控系统"],
         traits=["no_education_section", "vague_employer_name", "missing_dates", "thin_content"],
         expected=dict(c_lang="SUPPORTED", c_dev5="NEEDS_REVIEW", c_dir3="NEEDS_REVIEW",
                       c_stream="NOT_EVIDENCED", p_risk_stream="NOT_EVIDENCED", p_lead="NOT_EVIDENCED"),
         note="对抗点:无教育经历、公司名模糊('某互联网金融服务公司')、项目无日期;资料缺口应进入'待确认问题',不得脑补。"),

    dict(id="CV17", name="袁明", title="资深大数据专家", layout="normal",
         phone="139-2217-1717", email="yuan.ming.bd@example.com",
         summary="12 年软件开发经验,10 年大数据方向,历任开发、架构、技术管理,项目经历横跨电商、支付、证券风控。",
         works=[
             dict(org="上海证券信息技术有限公司", role="大数据技术总监", period="2021.06 - 至今",
                  points=["负责证券风控与交易分析数据体系的整体技术规划,管理 20 人研发团队。"]),
             dict(org="支付宝(中国)网络技术有限公司", role="资深技术专家", period="2017.03 - 2021.05",
                  points=["负责支付风控数据平台架构,主导流式特征体系建设。"]),
             dict(org="北京京东世纪贸易有限公司", role="大数据架构师", period="2014.05 - 2017.02",
                  points=["负责电商大促实时数据链路架构。"]),
             dict(org="用友网络科技股份有限公司", role="Java 开发工程师", period="2013.07 - 2014.04",
                  points=["从事企业级应用后端开发。"]),
         ],
         projects=[
             dict(name="证券实时风控数据平台", role="技术负责人", period="2022.01 - 至今",
                  stack="Flink、Java、Kafka、StarRocks、Hive",
                  points=["主导平台方案设计:账户实时风险画像基于 Flink 流式加工,风控指标查询延迟 P99 小于 50ms。",
                          "建设流批一体的指标口径管理体系,离线回算与实时链路共用一套指标定义。"]),
             dict(name="支付风控流式特征平台", role="架构师", period="2018.05 - 2021.03",
                  stack="Flink、Java、HBase、Kafka",
                  points=["主导流式特征平台架构,支撑反欺诈模型在线特征与离线训练特征的一致性。"]),
             dict(name="交易反洗钱实时监测", role="架构师", period="2019.09 - 2020.12",
                  stack="Flink、Spark、Java",
                  points=["设计实时大额与可疑交易监测链路,流式命中规则实时上报,批量模型 T+1 回扫。"]),
             dict(name="大促实时数据作战室", role="架构师", period="2015.10 - 2016.12",
                  stack="Storm、Java、Redis、ClickHouse",
                  points=["负责大促期间实时交易大屏与容量水位监控链路。"]),
             dict(name="供应链金融数据中台", role="核心开发", period="2021.09 - 2023.06",
                  stack="Spark、Hive、Java、Spring Cloud",
                  points=["负责中台离线数仓与数据服务接口开发。"]),
             dict(name="支付反欺诈模型特征管道", role="架构师", period="2018.09 - 2020.06",
                  stack="Flink、Spark、HBase、Java",
                  points=["设计模型在线/离线特征一致性方案:在线特征走 Flink 流式加工,离线训练样本由 Spark 批处理回刷。",
                          "建设特征血缘与版本管理,支撑反欺诈模型周级迭代。"]),
             dict(name="证券行情实时接入平台", role="技术负责人", period="2022.03 - 2024.09",
                  stack="Java、Kafka、Flink、TimescaleDB",
                  points=["主导行情接入层方案设计:交易所行情经 Kafka 分发,Flink 完成实时快照与分钟线聚合。",
                          "设计行情数据的多活容灾链路,故障切换时间小于 30 秒。"]),
             dict(name="集团统一指标服务平台", role="架构师", period="2016.01 - 2017.01",
                  stack="Java、Redis、MySQL、Spring",
                  points=["负责指标定义与查询服务模块开发,统一各业务线指标出口。"]),
         ],
         skills=["精通 Java", "精通 Flink,熟悉 Spark、Storm", "熟悉 Kafka、HBase、StarRocks、ClickHouse",
                 "具备大型风控数据体系架构与技术管理经验", "熟悉证券、支付、电商业务领域"],
         education=[dict(school="北京航空航天大学", major="计算机科学与技术", degree="硕士", period="2010.09 - 2013.06")],
         traits=["long_dense_multipage", "clean_positive", "multi_era_projects"],
         expected=dict(c_lang="SUPPORTED", c_dev5="SUPPORTED", c_dir3="SUPPORTED",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="SUPPORTED"),
         note="对抗点:内容长、chunk 多、时间跨度大(Storm 时代与 Flink 时代并存);召回与归属必须按项目分,不得把早期 Storm 经历算进 Flink 能力证据。"),

    dict(id="CV18", name="杜鹃", title="Java 后端工程师", layout="normal",
         phone="158-2218-1818", email="du.juan.crud@example.com",
         summary="5 年企业信息化系统开发经验。",
         works=[
             dict(org="苏州科达科技股份有限公司", role="Java 开发工程师", period="2020.07 - 至今",
                  points=["负责公司内部 OA 与行政管理系统的开发维护。"]),
         ],
         projects=[
             dict(name="企业 OA 办公系统", role="开发", period="2020.09 - 至今",
                  stack="Java、Spring Boot、MySQL、Vue",
                  points=["负责审批流程、考勤与公文管理模块的功能开发与缺陷修复。",
                          "维护系统日常运行,处理用户反馈问题。"]),
         ],
         skills=["熟悉 Java、Spring Boot", "熟悉 MySQL", "了解 Vue 前端开发"],
         education=[dict(school="苏州大学", major="软件工程", degree="本科", period="2016.09 - 2020.06")],
         traits=["fully_unrelated_control", "no_bigdata_no_risk"],
         expected=dict(c_lang="SUPPORTED", c_dev5="PARTIAL", c_dir3="NOT_EVIDENCED",
                       c_stream="NOT_EVIDENCED", p_risk_stream="NOT_EVIDENCED", p_lead="NOT_EVIDENCED"),
         note="对照组:纯管理后台 CRUD,无大数据无风控;全部领域条件应为 NOT_EVIDENCED(而非'不具备'),且不应进入前 20 高分。"),

    dict(id="CV19", name="冯倩", title="实时风控开发", layout="normal",
         phone="187-2219-1919", email="feng.qian.rt@example.com",
         summary="3 年开发经验,其中 2 年专注 Flink 实时风控。",
         works=[
             dict(org="北京京东数科科技有限公司", role="大数据开发工程师", period="2022.07 - 至今",
                  points=["在风控技术部从事实时风控链路开发。"]),
         ],
         projects=[
             dict(name="零售风控实时决策链路", role="开发", period="2023.01 - 至今",
                  stack="Flink、Java、Kafka、Redis",
                  points=["负责交易实时风控指标的 Flink 作业开发:滑动窗口聚合、状态后端调优与灰度上线。",
                          "参与实时规则引擎对接,支撑营销反作弊场景的秒级拦截。"]),
         ],
         skills=["熟悉 Flink 实时开发", "熟悉 Java", "了解风控业务"],
         education=[dict(school="天津工业大学", major="数据科学与大数据技术", degree="本科", period="2018.09 - 2022.06")],
         traits=["preferred_met_hard_years_unmet", "junior_profile"],
         expected=dict(c_lang="SUPPORTED", c_dev5="NOT_EVIDENCED", c_dir3="SUPPORTED",
                       c_stream="SUPPORTED", p_risk_stream="SUPPORTED", p_lead="NOT_EVIDENCED"),
         note="对抗点:优先项(实时风控流批经验)满足,但总开发年限仅 3 年、方向年限 2 年,均低于硬条件;不得因加分项好而放行硬年限。"),

    dict(id="CV20", name="蒋博", title="大数据平台工程师", layout="normal",
         phone="176-2220-2020", email="jiang.bo.platform@example.com",
         summary="多年大数据领域从业经验,深度参与公司大数据体系建设。",
         works=[
             dict(org="北京百分点信息科技有限公司", role="大数据平台工程师", period="2017.05 - 至今",
                  points=["深度参与公司大数据体系建设,负责平台相关研发工作。",
                          "熟悉各类流批处理技术,具备丰富的实时数据处理相关经验。"]),
         ],
         projects=[
             dict(name="公司大数据体系", role="研发", period="",
                  stack="",
                  points=["参与大数据平台多个模块的研发工作,涉及数据接入、存储与计算等环节。",
                          "配合团队完成实时与离线数据处理相关任务。"]),
         ],
         skills=["熟悉各类流批处理技术", "具备丰富的实时数据处理经验", "熟悉大数据生态组件"],
         traits=["vague_unlocatable_claims", "no_concrete_project", "no_stack_no_dates"],
         expected=dict(c_lang="NOT_EVIDENCED", c_dev5="NEEDS_REVIEW", c_dir3="NOT_EVIDENCED",
                       c_stream="NEEDS_REVIEW", p_risk_stream="NOT_EVIDENCED", p_lead="NOT_EVIDENCED"),
         note="对抗点:通篇'深度参与/熟悉各类',无具体项目、技术栈、日期;能力声明无法定位到具体经历,只能待确认,不得当作明确支持。"),
]

# ---------------------------------------------------------------- 渲染

S_NAME = ParagraphStyle("name", fontName=FONT, fontSize=20, leading=26, spaceAfter=2)
S_TITLE = ParagraphStyle("title", fontName=FONT, fontSize=11, leading=15,
                         textColor=colors.HexColor("#444444"))
S_CONTACT = ParagraphStyle("contact", fontName=FONT, fontSize=9.5, leading=14,
                           textColor=colors.HexColor("#555555"))
S_SEC = ParagraphStyle("sec", fontName=FONT, fontSize=13, leading=18, spaceBefore=10,
                       spaceAfter=2, textColor=colors.HexColor("#1a3c6e"))
S_ORG = ParagraphStyle("org", fontName=FONT, fontSize=11, leading=15, spaceBefore=6)
S_META = ParagraphStyle("meta", fontName=FONT, fontSize=9.5, leading=13,
                        textColor=colors.HexColor("#555555"))
S_BODY = ParagraphStyle("body", fontName=FONT, fontSize=10, leading=15)
S_ITEM = ParagraphStyle("item", fontName=FONT, fontSize=10, leading=15,
                        leftIndent=10, bulletIndent=0, spaceAfter=1)


def section(title):
    return [Paragraph(title, S_SEC),
            HRFlowable(width="100%", thickness=0.7,
                       color=colors.HexColor("#1a3c6e"), spaceAfter=4)]


def story_for(c, colbreak=False):
    f = []
    f.append(Paragraph(c["name"], S_NAME))
    f.append(Paragraph(c["title"], S_TITLE))
    f.append(Paragraph(f"电话:{c['phone']}    邮箱:{c['email']}", S_CONTACT))
    f.append(Spacer(1, 4))
    if c.get("summary"):
        f += section("个人简介")
        f.append(Paragraph(c["summary"], S_BODY))
    f += section("工作经历")
    for w in c["works"]:
        f.append(Paragraph(f"<b>{w['org']}</b>", S_ORG))
        f.append(Paragraph(f"{w['role']}    {w['period']}", S_META))
        for p in w["points"]:
            f.append(Paragraph(f"• {p}", S_ITEM))
    if colbreak:
        from reportlab.platypus import FrameBreak
        f.append(FrameBreak())
    if c.get("projects"):
        f += section("项目经历")
        for p in c["projects"]:
            head = f"<b>{p['name']}</b>"
            f.append(Paragraph(head, S_ORG))
            meta = p["role"] + (f"    {p['period']}" if p["period"] else "")
            f.append(Paragraph(meta, S_META))
            if p.get("stack"):
                f.append(Paragraph(f"技术栈:{p['stack']}", S_META))
            for d in p["points"]:
                f.append(Paragraph(f"• {d}", S_ITEM))
    if c.get("skills"):
        f += section("专业技能")
        for s in c["skills"]:
            f.append(Paragraph(f"• {s}", S_ITEM))
    if c.get("education"):
        f += section("教育经历")
        for e in c["education"]:
            f.append(Paragraph(f"{e['school']}    {e['major']}    {e['degree']}    {e['period']}",
                               S_BODY))
    return f


def render_normal(c, path):
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"{c['name']}-{c['title']}", author=c["name"])
    doc.build(story_for(c))


def render_two_col(c, path):
    doc = BaseDocTemplate(str(path), pagesize=A4,
                          leftMargin=14 * mm, rightMargin=14 * mm,
                          topMargin=14 * mm, bottomMargin=14 * mm,
                          title=f"{c['name']}-{c['title']}", author=c["name"])
    pw, ph = A4
    colw = (pw - 14 * mm * 2 - 8 * mm) / 2
    colh = ph - 14 * mm * 2
    left = Frame(14 * mm, 14 * mm, colw, colh, id="L",
                 leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    right = Frame(14 * mm + colw + 8 * mm, 14 * mm, colw, colh, id="R",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="twocol", frames=[left, right])])
    doc.build(story_for(c, colbreak=True))


def plain_lines(c):
    """扫描版用的纯文本行(模拟打印后再扫描的简历)。"""
    lines = [f"{c['name']}  {c['title']}", f"电话:{c['phone']}  邮箱:{c['email']}", ""]
    if c.get("summary"):
        lines += ["【个人简介】", c["summary"], ""]
    lines.append("【工作经历】")
    for w in c["works"]:
        lines += [f"{w['org']}  {w['role']}  {w['period']}"] + list(w["points"]) + [""]
    if c.get("projects"):
        lines.append("【项目经历】")
        for p in c["projects"]:
            lines.append(f"{p['name']}  {p['role']}  {p['period']}")
            if p.get("stack"):
                lines.append(f"技术栈:{p['stack']}")
            lines += list(p["points"]) + [""]
    if c.get("skills"):
        lines.append("【专业技能】")
        lines += c["skills"] + [""]
    if c.get("education"):
        lines.append("【教育经历】")
        for e in c["education"]:
            lines.append(f"{e['school']}  {e['major']}  {e['degree']}  {e['period']}")
    return lines


def render_scanned(c, path):
    from PIL import Image, ImageDraw, ImageFont
    ttf = "/System/Library/Fonts/Supplemental/Songti.ttc"
    font = ImageFont.truetype(ttf, 24, index=0)
    W, H = 1240, 1754  # A4 @150dpi
    margin, lh = 70, 38
    per_page = (H - 2 * margin) // lh
    lines = plain_lines(c)
    pages = []
    for i in range(0, max(1, len(lines)), per_page):
        img = Image.new("RGB", (W, H), "#f6f4ef")  # 略带底色的扫描件效果
        d = ImageDraw.Draw(img)
        y = margin
        for ln in lines[i:i + per_page]:
            # 简单硬折行(中文按字符数)
            while len(ln) > 44:
                d.text((margin, y), ln[:44], font=font, fill="#1c1c1c")
                ln, y = ln[44:], y + lh
            d.text((margin, y), ln, font=font, fill="#1c1c1c")
            y += lh
        pages.append(img)
    pages[0].save(str(path), "PDF", resolution=150.0, save_all=True,
                  append_images=pages[1:])


RENDERERS = dict(normal=render_normal, two_col=render_two_col, scan=render_scanned)


def main():
    RES_DIR.mkdir(parents=True, exist_ok=True)
    GT_DIR.mkdir(parents=True, exist_ok=True)
    index = []
    for c in CANDIDATES:
        fname = f"{c['id']}_{c['name']}_{c['title'].replace('/', '-')}.pdf"
        path = RES_DIR / fname
        RENDERERS[c["layout"]](c, path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        gt = {k: c[k] for k in
              ("id", "name", "title", "layout", "traits", "expected") if k in c}
        gt.update(filename=fname, sha256=sha, synthetic=True,
                  note=c.get("note", ""), jd_anchor="中信期货 J10034 应用架构师(风控平台方向)")
        (GT_DIR / f"{c['id']}.json").write_text(
            json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
        index.append({k: gt[k] for k in ("id", "filename", "sha256", "layout", "traits")})
        print(f"{c['id']} {fname}  layout={c['layout']}  sha={sha[:12]}…")
    (GT_DIR / "index.json").write_text(
        json.dumps(dict(count=len(index), synthetic=True, files=index),
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成:{len(index)} 份 → {RES_DIR}\nground truth → {GT_DIR}")


if __name__ == "__main__":
    main()
