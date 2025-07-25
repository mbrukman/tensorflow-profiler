/* Copyright 2024 The TensorFlow Authors. All Rights Reserved.

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

#ifndef XPROF_CONVERT_OP_STATS_TO_HLO_STATS_H_
#define XPROF_CONVERT_OP_STATS_TO_HLO_STATS_H_

#include <memory>
#include <string>

#include "xprof/convert/data_table_utils.h"
#include "plugin/xprof/protobuf/hlo_stats.pb.h"
#include "plugin/xprof/protobuf/op_stats.pb.h"

namespace tensorflow {
namespace profiler {
tensorflow::profiler::hlo_stats::HloStatsDatabase ConvertOpStatsToHloStats(
    const tensorflow::profiler::OpStats& op_stats);

// Converts to JSON align with current DataTable JSON format.
std::string HloStatsToDataTableJson(
    const hlo_stats::HloStatsDatabase& hlo_stats_db);

// Construct a DataTable object from HloStatsDatabase.
std::unique_ptr<tensorflow::profiler::DataTable> CreateHloStatsDataTable(
    const hlo_stats::HloStatsDatabase& hlo_stats_db);

}  // namespace profiler
}  // namespace tensorflow

#endif  // XPROF_CONVERT_OP_STATS_TO_HLO_STATS_H_
