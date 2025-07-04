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
#ifndef XPROF_CONVERT_MULTI_XSPACE_TO_INFERENCE_STATS_H_
#define XPROF_CONVERT_MULTI_XSPACE_TO_INFERENCE_STATS_H_

#include "absl/status/status.h"
#include "absl/strings/string_view.h"
#include "xprof/convert/repository.h"
#include "plugin/xprof/protobuf/inference_stats.pb.h"
#include "xprof/utils/event_span.h"

namespace tensorflow::profiler {
// Get non overlapped step events from xspace for GPU.
StepEvents GetNonOverlappedStepEvents(XSpace* xspace);

absl::Status ConvertMultiXSpaceToInferenceStats(
    const SessionSnapshot& session_snapshot, absl::string_view request_column,
    absl::string_view batch_column, InferenceStats* inference_stats);
}  // namespace tensorflow::profiler

#endif  // XPROF_CONVERT_MULTI_XSPACE_TO_INFERENCE_STATS_H_
