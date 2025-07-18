/* Copyright 2019 The TensorFlow Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#include "xprof/utils/op_metrics_db_utils.h"

#include <algorithm>
#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <utility>

#include "absl/container/flat_hash_map.h"
#include "absl/log/check.h"
#include "absl/log/log.h"
#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/numbers.h"
#include "absl/strings/str_cat.h"
#include "absl/strings/string_view.h"
#include "xla/tsl/platform/logging.h"
#include "xla/tsl/platform/types.h"
#include "xla/tsl/profiler/utils/math_utils.h"
#include "xla/tsl/profiler/utils/tf_op_utils.h"
#include "xla/tsl/profiler/utils/xplane_schema.h"
#include "xla/tsl/profiler/utils/xplane_visitor.h"
#include "plugin/xprof/protobuf/op_metrics.pb.h"
#include "plugin/xprof/protobuf/source_info.pb.h"

namespace tensorflow {
namespace profiler {
using tsl::uint64;

const absl::string_view kIdle = "IDLE";
const uint32_t kSparseCoreIndexStart = 1000000;
const int64_t kSingleOccurrence = 1;

namespace {

constexpr uint64_t kRootSymbolId = 0;

using tsl::profiler::StatType;
using tsl::profiler::XEventMetadataVisitor;
using tsl::profiler::XStatVisitor;

// Extracts the source filename and line from the input `source_top_line`, which
// is in the format of `<source_filename>:<source_line_number>`.
absl::StatusOr<std::pair<std::string, int32_t>>
ExtractSourceFileNameAndLineNumber(absl::string_view source_top_line) {
  const auto delimiterPos = source_top_line.find(':');
  if (delimiterPos == std::string_view::npos) {
    return absl::InvalidArgumentError(
        absl::StrCat("Invalid source info expression: '",
                     std::string(source_top_line), "'"));
  }
  const auto source_file = source_top_line.substr(0, delimiterPos);
  const auto line_str = source_top_line.substr(delimiterPos + 1);
  int32_t source_line;
  if (!absl::SimpleAtoi(line_str, &source_line)) {
    return absl::InvalidArgumentError(
        absl::StrCat("Invalid source line: '", std::string(line_str), "'"));
  }
  return std::make_pair(std::string(source_file), source_line);
}

// Populates the source filename and line number in the `op_metrics` from the
// input `source_top_line`, which is expected to be in the format of
// `<source_filename>:<source_line_number>`. If the `source_top_line` is not in
// the expected format, then `op_metrics` will not be populated.`
void PopulateSourceInfo(absl::string_view source_top_line,
                        SourceInfo& source_info) {
  const auto file_and_line =
      ExtractSourceFileNameAndLineNumber(source_top_line);
  if (file_and_line.ok()) {
    source_info.set_file_name(std::move(file_and_line->first));
    source_info.set_line_number(file_and_line->second);
  } else {
    LOG(ERROR) << "Failed to extract source filename and line from the input "
                  "source_top_line: '"
               << source_top_line
               << "' with status: " << file_and_line.status();
  }
}

class DeviceTfOpMetricsDbBuilder : public OpMetricsDbBuilder {
 public:
  explicit DeviceTfOpMetricsDbBuilder(OpMetricsDb* db)
      : OpMetricsDbBuilder(db) {}

  void UpdateTfOpMetricsWithDeviceOpMetrics(
      absl::string_view tf_op_name, absl::string_view tf_op_type,
      const OpMetrics& device_op_metrics) {
    OpMetrics* tf_op_metrics = OpMetricsDbBuilder::LookupOrInsertNewOpMetrics(
        /*hlo_module_id=*/0, tf_op_name);
    if (tf_op_metrics->category().empty()) {
      tf_op_metrics->set_category(tf_op_type == tsl::profiler::kUnknownOp
                                      ? "Unknown"
                                      : std::string(tf_op_type));
    }
    tf_op_metrics->set_is_eager(device_op_metrics.is_eager());
    // The occurrences of a TF-op is the maximum among the occurrences of all
    // device ops that it contains.
    tf_op_metrics->set_occurrences(std::max(tf_op_metrics->occurrences(),
                                            device_op_metrics.occurrences()));
    tf_op_metrics->set_time_ps(tf_op_metrics->time_ps() +
                               device_op_metrics.time_ps());
    tf_op_metrics->set_self_time_ps(tf_op_metrics->self_time_ps() +
                                    device_op_metrics.self_time_ps());
    tf_op_metrics->set_flops(tf_op_metrics->flops() +
                             device_op_metrics.flops());
    tf_op_metrics->set_model_flops(tf_op_metrics->model_flops() +
                                    device_op_metrics.model_flops());
    tf_op_metrics->set_bytes_accessed(tf_op_metrics->bytes_accessed() +
                                      device_op_metrics.bytes_accessed());
  }
};

void SetOpMetadataFromHloEventMetadata(
    const XEventMetadataVisitor& hlo_event_metadata, OpMetrics* op_metrics) {
  if (hlo_event_metadata.HasDisplayName()) {
    op_metrics->set_name(std::string(hlo_event_metadata.DisplayName()));
    op_metrics->set_long_name(std::string(hlo_event_metadata.Name()));
  } else {
    op_metrics->set_name(std::string(hlo_event_metadata.Name()));
  }
  hlo_event_metadata.ForEachStat([&](const XStatVisitor& stat) {
    if (stat.Type().has_value()) {
      switch (static_cast<StatType>(*stat.Type())) {
        case StatType::kProgramId:
          op_metrics->set_hlo_module_id(stat.IntOrUintValue());
          break;
        case StatType::kHloCategory:
          op_metrics->set_category(std::string(stat.StrOrRefValue()));
          break;
        case StatType::kTfOp:
          op_metrics->set_provenance(std::string(stat.StrOrRefValue()));
          break;
        case StatType::kFlops:
          op_metrics->set_flops(stat.IntOrUintValue());
          break;
        case StatType::kModelFlops:
          op_metrics->set_model_flops(stat.IntOrUintValue());
          break;
        case StatType::kBytesAccessed:
          op_metrics->set_bytes_accessed(stat.IntOrUintValue());
          break;
        case StatType::kMemoryAccessBreakdown: {
          tensorflow::profiler::MemoryAccessBreakdown breakdown;
          const auto& value = stat.BytesValue();
          if (breakdown.ParseFromArray(value.data(), value.size())) {
            *op_metrics->mutable_memory_accessed_breakdown() =
                breakdown.memory_accessed();
          }
          break;
        }
        case StatType::kDeduplicatedName:
          op_metrics->set_deduplicated_name(std::string(stat.StrOrRefValue()));
          break;
        case StatType::kSourceInfo:
          PopulateSourceInfo(stat.StrOrRefValue(),
                             *op_metrics->mutable_source_info());
          break;
        case StatType::kSourceStack:
          op_metrics->mutable_source_info()->set_stack_frame(
              std::string(stat.StrOrRefValue()));
          break;
        default:
          break;
      }
    }
  });
  hlo_event_metadata.ForEachChild(
      [&](const XEventMetadataVisitor& child_hlo_event_metadata) {
        OpMetrics* child = op_metrics->mutable_children()->add_metrics_db();
        child->set_occurrences(1);
        SetOpMetadataFromHloEventMetadata(child_hlo_event_metadata, child);
      });
}

void SetOpMetricsFromHloEvent(const tsl::profiler::XEventVisitor& hlo_event,
                              OpMetrics* op_metrics) {
  uint64_t duration_ps = hlo_event.DurationPs();
  uint64_t min_duration_ps = duration_ps;
  uint64_t self_duration_ps = duration_ps;
  uint64_t dma_stall_ps = 0;
  hlo_event.ForEachStat([&](const XStatVisitor& stat) {
    if (!stat.Type()) return;
    switch (static_cast<StatType>(*stat.Type())) {
      case StatType::kMinDurationPs:
        min_duration_ps = stat.IntValue();
        break;
      case StatType::kSelfDurationPs:
        self_duration_ps = stat.IntValue();
        break;
      case StatType::kDmaStallDurationPs:
        dma_stall_ps = stat.IntValue();
        break;
      default:
        break;
    }
  });
  if (op_metrics->occurrences() == 0) {
    SetOpMetadataFromHloEventMetadata(hlo_event.Metadata(), op_metrics);
    op_metrics->set_occurrences(
        std::max(kSingleOccurrence, hlo_event.NumOccurrences()));
    op_metrics->set_time_ps(duration_ps);
    op_metrics->set_min_time_ps(min_duration_ps);
    op_metrics->set_self_time_ps(self_duration_ps);
    op_metrics->set_dma_stall_ps(dma_stall_ps);
    op_metrics->set_num_cores(1);
  } else {
    op_metrics->set_occurrences(op_metrics->occurrences() +
                                hlo_event.NumOccurrences());
    op_metrics->set_time_ps(op_metrics->time_ps() + duration_ps);
    op_metrics->set_min_time_ps(
        std::min<uint64_t>(op_metrics->min_time_ps(), min_duration_ps));
    op_metrics->set_self_time_ps(op_metrics->self_time_ps() + self_duration_ps);
    op_metrics->set_dma_stall_ps(op_metrics->dma_stall_ps() + dma_stall_ps);
  }
}

void MergeOpMetrics(const OpMetrics& src, OpMetrics& dst) {
  if (dst.occurrences() == 0) {
    dst = src;
  } else {
    dst.set_occurrences(src.occurrences() + dst.occurrences());
    dst.set_time_ps(src.time_ps() + dst.time_ps());
    dst.set_min_time_ps(
        std::min<uint64_t>(src.min_time_ps(), dst.min_time_ps()));
    dst.set_self_time_ps(src.self_time_ps() + dst.self_time_ps());
    dst.set_dma_stall_ps(src.dma_stall_ps() + dst.dma_stall_ps());
  }
}

void AdjustFlopsAndBytesAccessed(OpMetrics& op_metrics) {
  op_metrics.set_flops(op_metrics.flops() * op_metrics.occurrences());
  if (op_metrics.model_flops() > 0) {
    op_metrics.set_model_flops(op_metrics.model_flops() *
                               op_metrics.occurrences());
  } else {
    op_metrics.set_model_flops(op_metrics.flops());
  }
  op_metrics.set_bytes_accessed(op_metrics.bytes_accessed() *
                                op_metrics.occurrences());
  for (auto& memory_access : *op_metrics.mutable_memory_accessed_breakdown()) {
    memory_access.set_bytes_accessed(memory_access.bytes_accessed() *
                                     op_metrics.occurrences());
  }
}

}  // namespace

OpMetricsDbBuilder::OpMetricsDbBuilder(OpMetricsDb* db) : db_(db) {
  DCHECK_NE(db_, nullptr);
  DCHECK_EQ(db_->metrics_db_size(), db->metrics_db_size());
}

OpMetrics* OpMetricsDbBuilder::LookupOrInsertNewOpMetrics(
    uint64 hlo_module_id, absl::string_view name) {
  OpMetrics*& op_metrics = op_metrics_map_[hlo_module_id][name];
  if (op_metrics == nullptr) {
    op_metrics = db_->add_metrics_db();
    op_metrics->set_hlo_module_id(hlo_module_id);
    op_metrics->set_name(name.data(), name.size());
  }
  return op_metrics;
}

void XEventsOpMetricsDbBuilder::AddOpMetric(
    const tsl::profiler::XEventVisitor& event) {
  AddOpMetric(FromXEvent(event), GetOpKeyFromXEvent(event));
}

void XEventsOpMetricsDbBuilder::AddOpMetric(const OpMetrics& op_metrics,
                                            const OpKey& key) {
  if (!key.program_id.has_value() || !key.symbol_id.has_value() ||
      key.symbol_id == kRootSymbolId)
    return;
  MergeOpMetrics(
      op_metrics,
      flat_op_metric_[key.program_id.value()][key.symbol_id.value()]);
}

OpMetricsDb XEventsOpMetricsDbBuilder::Finalize(uint64_t total_time_ps) {
  OpMetricsDb db = Finalize();
  SetTotalTimePs(db, total_time_ps);
  AddIdleOp(db);
  return db;
}

OpMetricsDb XEventsOpMetricsDbBuilder::Finalize() {
  OpMetricsDb db;
  uint64_t total_op_time_ps = 0;
  for (auto& [program_id, op_metric_by_symbol] : flat_op_metric_) {
    for (auto& [symbol_id, op_metrics] : op_metric_by_symbol) {
      AdjustFlopsAndBytesAccessed(op_metrics);
      total_op_time_ps += op_metrics.self_time_ps();
      db.add_metrics_db()->Swap(&op_metrics);
    }
  }
  db.set_total_op_time_ps(total_op_time_ps);
  return db;
}

double IdleTimeRatio(const OpMetricsDb& db) {
  return 1.0 -
         tsl::profiler::SafeDivide(db.total_op_time_ps(), db.total_time_ps());
}

uint64 IdleTimePs(const OpMetricsDb& db) {
  DCHECK_GE(db.total_time_ps(), db.total_op_time_ps());
  return db.total_time_ps() - db.total_op_time_ps();
}

void SetIdleOp(uint64_t idle_time_ps, OpMetrics& metrics) {
  metrics.set_name(std::string(kIdle));
  metrics.set_category(std::string(kIdle));
  metrics.set_occurrences(0);
  metrics.set_time_ps(idle_time_ps);
  metrics.set_self_time_ps(idle_time_ps);
}

void AddIdleOp(OpMetricsDb& db) {
  uint64 idle_time_ps = IdleTimePs(db);
  SetIdleOp(idle_time_ps, *db.add_metrics_db());
}

std::optional<double> HostInfeedEnqueueRatio(const OpMetricsDb& db) {
  if (db.total_host_infeed_enq_start_timestamp_ps_diff() > 0) {
    // We use total_host_infeed_enq_start_timestamp_ps_diff to approximate the
    // total host time.
    return tsl::profiler::SafeDivide(
        db.total_host_infeed_enq_duration_ps(),
        db.total_host_infeed_enq_start_timestamp_ps_diff());
  }
  return std::nullopt;
}

OpMetricsDb CreateTfMetricsDbFromDeviceOpMetricsDb(
    const OpMetricsDb& device_op_metrics_db, bool with_idle) {
  OpMetricsDb tf_op_metrics_db;
  DeviceTfOpMetricsDbBuilder builder(&tf_op_metrics_db);
  for (const auto& device_op_metrics : device_op_metrics_db.metrics_db()) {
    if (IsIdleOp(device_op_metrics)) {
      if (with_idle) {
        builder.UpdateTfOpMetricsWithDeviceOpMetrics(kIdle, kIdle,
                                                     device_op_metrics);
      }
    } else if (device_op_metrics.provenance().empty()) {
      builder.UpdateTfOpMetricsWithDeviceOpMetrics(device_op_metrics.name(),
                                                   tsl::profiler::kUnknownOp,
                                                   device_op_metrics);
    } else {
      tsl::profiler::TfOp tf_op =
          tsl::profiler::ParseTfOpFullname(device_op_metrics.provenance());
      builder.UpdateTfOpMetricsWithDeviceOpMetrics(tf_op.name, tf_op.type,
                                                   device_op_metrics);
    }
  }
  tf_op_metrics_db.set_total_op_time_ps(
      device_op_metrics_db.total_op_time_ps());

  tf_op_metrics_db.set_total_time_ps(
      with_idle ? device_op_metrics_db.total_time_ps()
                : device_op_metrics_db.total_op_time_ps());

  return tf_op_metrics_db;
}

OpMetrics FromXEvent(const tsl::profiler::XEventVisitor& xevent) {
  OpMetrics op_metrics;
  std::optional<XStatVisitor> stat = xevent.GetStat(StatType::kStepIdleTimePs);
  if (stat.has_value()) {
    // TODO(b/397774568) : Remove this once the SparseCore OpMetricsDb is
    // implemented.
    uint64_t idle_time_ps = stat->IntOrUintValue();
    op_metrics.set_self_time_ps(xevent.DurationPs() - idle_time_ps);
    op_metrics.set_name("sparse_core_busy_ops");
    op_metrics.set_category("sparse_core_busy_ops");
    return op_metrics;
  }
  SetOpMetricsFromHloEvent(xevent, &op_metrics);
  return op_metrics;
}

XEventsOpMetricsDbBuilder::OpKey GetOpKeyFromXEvent(
    const tsl::profiler::XEventVisitor& event) {
  std::optional<XStatVisitor> stat = event.GetStat(StatType::kStepIdleTimePs);
  if (stat.has_value()) {
    return {.program_id = std::numeric_limits<uint64_t>::max(),
            .symbol_id = std::numeric_limits<uint64_t>::max()};
  }

  XEventsOpMetricsDbBuilder::OpKey op_key;
  DCHECK(event.metadata() != nullptr);
  event.Metadata().ForEachStat([&](const XStatVisitor& stat) {
    if (stat.Type().has_value()) {
      switch (static_cast<StatType>(*stat.Type())) {
        case StatType::kProgramId:
          op_key.program_id = stat.IntOrUintValue();
          break;
        case StatType::kSymbolId:
          op_key.symbol_id = stat.IntOrUintValue();
          break;
        default:
          break;
      }
    }
  });
  return op_key;
}

}  // namespace profiler
}  // namespace tensorflow
