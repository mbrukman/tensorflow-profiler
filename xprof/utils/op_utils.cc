/* Copyright 2020 The TensorFlow Authors. All Rights Reserved.

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

#include "xprof/utils/op_utils.h"

#include <cstdint>
#include <string>

#include "absl/log/check.h"
#include "absl/strings/string_view.h"
#include "xla/hlo/ir/hlo_opcode.h"
#include "xla/tsl/platform/types.h"
#include "xla/tsl/profiler/convert/xla_op_utils.h"
#include "xla/tsl/profiler/utils/tf_op_utils.h"
#include "xla/tsl/profiler/utils/timespan.h"
#include "tsl/platform/protobuf.h"
#include "xprof/convert/op_metrics_db_combiner.h"
#include "plugin/xprof/protobuf/op_metrics.pb.h"
#include "plugin/xprof/protobuf/source_info.pb.h"
#include "xprof/utils/hlo_module_map.h"
#include "xprof/utils/performance_info_wrapper.h"

namespace tensorflow {
namespace profiler {
using tsl::uint64;

tsl::protobuf::RepeatedPtrField<OpMetrics::MemoryAccessed>
ConvertPerformanceInfo(
    const tsl::protobuf::RepeatedPtrField<
        tensorflow::profiler::PerformanceInfoWrapper::PerfInfoType::
            MemoryAccessed>& memory_accessed_breakdown,
    uint64_t occurrences) {
  tsl::protobuf::RepeatedPtrField<OpMetrics::MemoryAccessed>
      memory_access_breakdown;
  for (const auto& m : memory_accessed_breakdown) {
    auto* memory_access = memory_access_breakdown.Add();
    memory_access->set_operation_type(m.is_read()
                                          ? OpMetrics::MemoryAccessed::READ
                                          : OpMetrics::MemoryAccessed::WRITE);
    memory_access->set_memory_space(m.memory_space());
    memory_access->set_bytes_accessed(m.bytes_accessed() * occurrences);
  }
  return memory_access_breakdown;
}

// Annotate the op_metrics with the metadata from the instr_wrapper.
void EnterOpMetadata(OpMetrics* op_metrics,
                     const HloInstructionWrapper* instr_wrapper) {
  if (op_metrics->name().empty() && op_metrics->category().empty() &&
      op_metrics->provenance().empty()) {
    op_metrics->set_name(std::string(instr_wrapper->Name()));
    op_metrics->set_category(std::string(instr_wrapper->Category()));
    op_metrics->set_deduplicated_name(
        instr_wrapper->Metadata().deduplicated_name());
    op_metrics->set_provenance(std::string(instr_wrapper->op_full_name()));
    op_metrics->set_num_cores(1);
    op_metrics->set_occurrences(op_metrics->occurrences() + 1);
    op_metrics->set_flops(op_metrics->flops() + instr_wrapper->flops());
    op_metrics->set_bytes_accessed(op_metrics->bytes_accessed() +
                                   instr_wrapper->bytes_accessed());
    op_metrics->set_long_name(instr_wrapper->Expression());
  }
}

void AddFusionChildrenToOpMetricsFromHloInstruction(
    OpMetrics* op_metrics, const HloInstructionWrapper* instr_wrapper) {
  if (instr_wrapper->FusedChildren().empty()) return;
  for (const HloInstructionWrapper* child : instr_wrapper->FusedChildren()) {
    if (child->HloOpcode() == xla::HloOpcode::kParameter ||
        child->HloOpcode() == xla::HloOpcode::kTuple)
      continue;
    OpMetrics* child_op_metrics =
        op_metrics->mutable_children()->add_metrics_db();
    EnterOpMetadata(child_op_metrics, child);
    AddFusionChildrenToOpMetricsFromHloInstruction(child_op_metrics, child);
  }
}

void EnterOpMetadataFromHloModuleMap(OpMetrics* op_metrics,
                                     const HloModuleMap& hlo_module_map) {
  const HloInstructionWrapper* instr_wrapper = GetHloInstruction(
      hlo_module_map, op_metrics->hlo_module_id(), op_metrics->name());
  if (instr_wrapper != nullptr) {
    AddFusionChildrenToOpMetricsFromHloInstruction(op_metrics, instr_wrapper);
  }
}

void HostOpMetricsDbBuilder::EnterOp(absl::string_view name,
                                     absl::string_view category, bool is_eager,
                                     uint64 time_ps, uint64 children_time_ps) {
  uint64 self_time_ps = time_ps - children_time_ps;
  DCHECK_GE(time_ps, self_time_ps);
  OpMetrics* op_metrics = LookupOrInsertNewOpMetrics(/*hlo_module_id=*/0, name);
  if (op_metrics->category().empty())
    op_metrics->set_category(category.data(), category.size());
  op_metrics->set_num_cores(1);
  op_metrics->set_is_eager(op_metrics->is_eager() || is_eager);
  op_metrics->set_occurrences(op_metrics->occurrences() + 1);
  op_metrics->set_time_ps(op_metrics->time_ps() + time_ps);
  op_metrics->set_self_time_ps(op_metrics->self_time_ps() + self_time_ps);
  db()->set_total_op_time_ps(db()->total_op_time_ps() + self_time_ps);
}

void HostOpMetricsDbBuilder::EnterHostInfeedEnqueue(
    tsl::profiler::Timespan host_infeed_enqueue) {
  if (!last_host_infeed_enqueue_.Empty()) {
    // Expect non-overlapping InfeedEnqueue timespans sorted by time.
    DCHECK_GE(host_infeed_enqueue.end_ps(),
              last_host_infeed_enqueue_.begin_ps());
    db()->set_total_host_infeed_enq_duration_ps(
        db()->total_host_infeed_enq_duration_ps() +
        last_host_infeed_enqueue_.duration_ps());
    db()->set_total_host_infeed_enq_start_timestamp_ps_diff(
        db()->total_host_infeed_enq_start_timestamp_ps_diff() +
        (host_infeed_enqueue.begin_ps() -
         last_host_infeed_enqueue_.begin_ps()));
  }
  last_host_infeed_enqueue_ = host_infeed_enqueue;
}

void DeviceOpMetricsDbBuilder::EnterOpMetadataFromHloModuleMap(
    uint64 program_id, absl::string_view op_name,
    const HloModuleMap& hlo_module_map) {
  OpMetrics* op_metrics = LookupOrInsertNewOpMetrics(program_id, op_name);
  tensorflow::profiler::EnterOpMetadataFromHloModuleMap(op_metrics,
                                                        hlo_module_map);
}

void DeviceOpMetricsDbBuilder::EnterOpMetadata(
    uint64 program_id, absl::string_view program_name,
    absl::string_view category, absl::string_view provenance,
    absl::string_view deduplicated_name, bool is_eager,
    absl::string_view long_name,
    const tsl::profiler::OpSourceInfo& op_source_info) {
  // We only need to add xla metadata once to each new op, as they are the
  // same across occurrences.
  OpMetrics* op_metrics = LookupOrInsertNewOpMetrics(program_id, program_name);
  if (op_metrics->occurrences() > 0 || !op_metrics->category().empty() ||
      !op_metrics->provenance().empty())
    return;
  op_metrics->set_category(category == tsl::profiler::kUnknownOp
                               ? "unknown"
                               : std::string(category));
  op_metrics->set_provenance(std::string(provenance));
  if (!deduplicated_name.empty()) {
    op_metrics->set_deduplicated_name(std::string(deduplicated_name));
  }
  if (!long_name.empty()) {
    op_metrics->set_long_name(std::string(long_name));
  }
  op_metrics->set_is_eager(op_metrics->is_eager() || is_eager);
  op_metrics->mutable_source_info()->set_file_name(
      std::string(op_source_info.source_file));
  op_metrics->mutable_source_info()->set_line_number(
      op_source_info.source_line);
  op_metrics->mutable_source_info()->set_stack_frame(
      op_source_info.stack_frame);
}

void DeviceOpMetricsDbBuilder::EnterOp(
    uint64 program_id, absl::string_view name, absl::string_view category,
    absl::string_view provenance, absl::string_view deduplicated_name,
    bool is_eager, uint64 occurrences, uint64 time_ps, uint64 children_time_ps,
    int64_t flops, int64_t bytes_accessed,
    // NOLINTNEXTLINE: clang-tidy missing-includes false positive
    const tsl::protobuf::RepeatedPtrField<OpMetrics::MemoryAccessed>&
        memory_accessed_breakdown,
    int64_t model_flops, absl::string_view long_name,
    const tsl::profiler::OpSourceInfo& op_source_info) {
  EnterOpMetadata(program_id, name, category, provenance, deduplicated_name,
                  is_eager, long_name, op_source_info);
  uint64 self_time_ps = time_ps - children_time_ps;
  DCHECK_GE(time_ps, self_time_ps);
  OpMetrics* op_metrics = LookupOrInsertNewOpMetrics(program_id, name);
  op_metrics->set_num_cores(1);
  op_metrics->set_occurrences(op_metrics->occurrences() + occurrences);
  op_metrics->set_time_ps(op_metrics->time_ps() + time_ps);
  op_metrics->set_self_time_ps(op_metrics->self_time_ps() + self_time_ps);
  op_metrics->set_flops(op_metrics->flops() + flops * occurrences);
  if (model_flops == 0) {
    // If ModelsFlops is 0, use the same value as device flops.
    op_metrics->set_model_flops(op_metrics->flops());
  } else {
    op_metrics->set_model_flops(op_metrics->model_flops() +
                                model_flops * occurrences);
  }
  op_metrics->set_bytes_accessed(op_metrics->bytes_accessed() +
                                 bytes_accessed * occurrences);
  CombineMemoryAccessedBreakdown(
      memory_accessed_breakdown,
      op_metrics->mutable_memory_accessed_breakdown());
  db()->set_total_op_time_ps(db()->total_op_time_ps() + self_time_ps);
}

}  // namespace profiler
}  // namespace tensorflow
