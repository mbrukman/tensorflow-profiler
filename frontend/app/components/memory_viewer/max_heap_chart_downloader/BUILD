load("@io_bazel_rules_sass//:defs.bzl", "sass_binary")
load("//defs:defs.bzl", "xprof_ng_module")

package(default_visibility = [
    "@org_xprof//frontend:__subpackages__",
])

xprof_ng_module(
    name = "max_heap_chart_downloader",
    srcs = [
        "max_heap_chart_downloader.ts",
        "max_heap_chart_downloader_module.ts",
    ],
    assets = [
        ":max_heap_chart_downloader_css",
        "max_heap_chart_downloader.ng.html",
    ],
    deps = [
        "@npm//@angular/common",
        "@npm//@angular/core",
        "@org_xprof//frontend/app/common/angular:angular_material_button",
        "@org_xprof//frontend/app/common/interfaces",
        "@org_xprof//frontend/app/components/memory_viewer/memory_usage",
    ],
)

sass_binary(
    name = "max_heap_chart_downloader_css",
    src = "max_heap_chart_downloader.scss",
    # stack = True,
    sourcemap = False,
    deps = [
    ],
)
