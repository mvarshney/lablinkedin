#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Pinot setup script — creates the 'impressions' table for impression tracking.
#
# Run AFTER Pinot is healthy:
#   ./scripts/init-pinot.sh
#
# The impressions table records (user_id, post_id, timestamp) for every post
# served to a user. The API queries this to filter seen posts in Stage 2.
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

PINOT_CONTROLLER=${PINOT_CONTROLLER_URL:-http://localhost:9002}

echo "Waiting for Pinot controller at $PINOT_CONTROLLER ..."
until curl -sf "$PINOT_CONTROLLER/health" > /dev/null; do
  sleep 3
done
echo "Pinot is ready."

# ── Add Schema ────────────────────────────────────────────────────────────────
echo "Creating impressions schema..."
curl -sf -X POST "$PINOT_CONTROLLER/schemas" \
  -H "Content-Type: application/json" \
  -d '{
    "schemaName": "impressions",
    "dimensionFieldSpecs": [
      { "name": "user_id", "dataType": "STRING" },
      { "name": "post_id", "dataType": "STRING" }
    ],
    "dateTimeFieldSpecs": [
      {
        "name": "timestamp",
        "dataType": "LONG",
        "format": "1:MILLISECONDS:EPOCH",
        "granularity": "1:MILLISECONDS"
      }
    ]
  }' | python3 -m json.tool || true

echo ""

# ── Add Table ─────────────────────────────────────────────────────────────────
echo "Creating impressions table..."
curl -sf -X POST "$PINOT_CONTROLLER/tables" \
  -H "Content-Type: application/json" \
  -d '{
    "tableName": "impressions",
    "tableType": "REALTIME",
    "segmentsConfig": {
      "timeColumnName": "timestamp",
      "timeType": "MILLISECONDS",
      "schemaName": "impressions",
      "replicasPerPartition": "1"
    },
    "tenants": {},
    "tableIndexConfig": {
      "loadMode": "MMAP",
      "streamConfigs": {
        "streamType": "kafka",
        "stream.kafka.consumer.type": "lowlevel",
        "stream.kafka.topic.name": "impressions",
        "stream.kafka.decoder.class.name": "org.apache.pinot.plugin.stream.kafka.KafkaJSONMessageDecoder",
        "stream.kafka.consumer.factory.class.name": "org.apache.pinot.plugin.stream.kafka20.KafkaConsumerFactory",
        "stream.kafka.broker.list": "kafka:9092",
        "realtime.segment.flush.threshold.rows": "50000",
        "realtime.segment.flush.threshold.time": "3600000",
        "stream.kafka.consumer.prop.auto.offset.reset": "smallest"
      }
    },
    "metadata": {
      "customConfigs": {}
    }
  }' | python3 -m json.tool || true

echo ""
echo "Pinot setup complete!"
echo "  Table: impressions (REALTIME, reading from Kafka topic 'impressions')"
echo "  Query via broker: http://localhost:8099/query/sql"
echo "  Controller UI:    $PINOT_CONTROLLER"
