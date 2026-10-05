import asyncio
import json
import logging
import os
import sys

# 确保 backend 目录在 path 中，以便能导入 app 模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from aiokafka import AIOKafkaConsumer

from app.core.config import settings
from app.worker.doc_processor import processor
from app.worker.heartbeat import touch_heartbeat

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 空闲时也要周期性回到循环里刷新心跳：getmany 即使没有消息也最多阻塞这么久
POLL_TIMEOUT_MS = 5000


async def run_consume_loop(consumer, process=None, touch=None):
    """消费循环：每次 Kafka 轮询返回后刷新心跳（issue #156）。

    用 ``while True`` + ``getmany(timeout_ms=...)`` 而不是 ``async for``：
    ``async for`` 在没有消息时会一直挂起，空闲（正常）状态反而刷不了心跳；
    ``getmany`` 即使没有消息也会按超时返回，于是空闲时心跳照常刷新，
    而进程冻结 / 事件循环停摆 / 消费推进被挂住时心跳停止刷新，
    容器健康检查（worker_healthcheck.py）即可判红。

    ``process`` / ``touch`` 仅用于测试注入，默认真实实现。
    """
    process_call = process or processor.process
    beat = touch or touch_heartbeat

    while True:
        batches = await consumer.getmany(timeout_ms=POLL_TIMEOUT_MS)
        beat()
        for records in batches.values():
            for msg in records:
                logger.info(f"Received task: {msg.value}")
                data = msg.value
                if not isinstance(data, dict):
                    # 非 dict 消息直接跳过并提交偏移，避免 AttributeError 杀死循环
                    logger.warning(f"Skipping non-dict message: {type(data).__name__}")
                    await consumer.commit()
                    continue

                doc_id = data.get("document_id")
                job_id = data.get("job_id")

                if doc_id:
                    try:
                        await process_call(doc_id, job_id)
                        # 处理成功后显式提交偏移；失败的消息不提交，重启后可重新消费
                        await consumer.commit()
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                else:
                    logger.warning("Received message without document_id")
                    await consumer.commit()


async def consume():
    """Kafka 消费者主循环"""
    logger.info(f"Connecting to Kafka at {settings.KAFKA_BOOTSTRAP_SERVERS}...")

    consumer = AIOKafkaConsumer(
        settings.KAFKA_FILE_PROCESSING_TOPIC,
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        group_id="doc_processor_group",
        value_deserializer=lambda m: json.loads(m.decode('utf-8')),
        auto_offset_reset="earliest",
        enable_auto_commit=False,  # 手动提交：处理成功后显式 commit，失败的消息可重新消费
    )

    try:
        # start() 也纳入 try/finally：启动失败时同样执行 stop()，避免连接泄漏
        await consumer.start()
        # 消费者就绪后立即落第一次心跳，健康检查从此刻开始盯存活性
        touch_heartbeat()
        logger.info("Kafka Consumer started. Waiting for messages...")

        await run_consume_loop(
            consumer,
            # 把心跳刷新作为处理阶段回调传入：单篇大文档的解析/向量化可能
            # 远超心跳阈值，阶段级刷新避免把"长耗时处理"误判为"卡死"。
            process=lambda doc_id, job_id: processor.process(
                doc_id, job_id, on_progress=touch_heartbeat
            ),
        )

    except Exception as e:
        logger.error(f"Consumer crashed: {e}")
    finally:
        logger.info("Stopping consumer...")
        try:
            await consumer.stop()
        except Exception as e:
            logger.error(f"Error stopping consumer: {e}")

if __name__ == "__main__":
    try:
        # Windows 上 asyncio 的 loop policy 问题
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        asyncio.run(consume())
    except KeyboardInterrupt:
        logger.info("Consumer stopped manually")
