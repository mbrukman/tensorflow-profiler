# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""The TensorBoard plugin for performance profiling."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
from collections.abc import Callable, Iterator
import gzip
import json
import logging
import os
import re
import threading
import time
from typing import Any, List, Optional, TypedDict

from etils import epath
from ratelimit import limits
from ratelimit import sleep_and_retry
import six
from werkzeug import wrappers

from xprof import version
from xprof.convert import raw_to_tool_data as convert
from xprof.standalone.tensorboard_shim import base_plugin
from xprof.standalone.tensorboard_shim import context as tb_context
from xprof.standalone.tensorboard_shim import plugin_asset_util


logger = logging.getLogger('tensorboard')

try:
  import tensorflow.compat.v2 as tf  # pylint: disable=g-import-not-at-top
  from tensorflow.python.profiler import profiler_client  # pylint: disable=g-direct-tensorflow-import,g-import-not-at-top
  from tensorflow.python.profiler import profiler_v2 as profiler  # pylint: disable=g-direct-tensorflow-import,g-import-not-at-top

  tf.enable_v2_behavior()
except ImportError:
  logger.info(
      'Disabling remote capture features as tensorflow is not available'
  )
  tf = None
  profiler_client = None
  profiler = None


# The prefix of routes provided by this plugin.
TB_NAME = 'plugins'
PLUGIN_NAME = 'profile'

BASE_ROUTE = '/'
INDEX_JS_ROUTE = '/index.js'
INDEX_HTML_ROUTE = '/index.html'
BUNDLE_JS_ROUTE = '/bundle.js'
STYLES_CSS_ROUTE = '/styles.css'
MATERIALICONS_WOFF2_ROUTE = '/materialicons.woff2'
TRACE_VIEWER_INDEX_HTML_ROUTE = '/trace_viewer_index.html'
TRACE_VIEWER_INDEX_JS_ROUTE = '/trace_viewer_index.js'
ZONE_JS_ROUTE = '/zone.js'
DATA_ROUTE = '/data'
RUNS_ROUTE = '/runs'
RUN_TOOLS_ROUTE = '/run_tools'
HOSTS_ROUTE = '/hosts'
HLO_MODULE_LIST_ROUTE = '/module_list'
CAPTURE_ROUTE = '/capture_profile'
LOCAL_ROUTE = '/local'
CACHE_VERSION_FILE = 'cache_version.txt'

# Suffixes of "^, #, @" symbols represent different input data formats for the
# same tool.
# 1) '^': data generate from XPlane.
# 2) '#': data is in gzip format.
# 3) '@': data generate from proto, or tracetable for streaming trace viewer.
# 4) no suffix: data is in json format, ready to feed to frontend.
TOOLS = {
    'xplane': 'xplane.pb',
    'hlo_proto': 'hlo_proto.pb',
}

ALL_HOSTS = 'ALL_HOSTS'

HostMetadata = TypedDict('HostMetadata', {'hostname': str})

_EXTENSION_TO_TOOL = {extension: tool for tool, extension in TOOLS.items()}

_FILENAME_RE = re.compile(r'(?:(.*)\.)?(' +
                          '|'.join(TOOLS.values()).replace('.', r'\.') + r')')


# Tools that can be generated from xplane end with ^.
XPLANE_TOOLS = [
    'trace_viewer',  # non-streaming before TF 2.13
    'trace_viewer@',  # streaming since TF 2.14
    'overview_page',
    'input_pipeline_analyzer',
    'framework_op_stats',
    'kernel_stats',
    'memory_profile',
    'pod_viewer',
    'op_profile',
    'hlo_stats',
    'roofline_model',
    'inference_profile',
    'memory_viewer',
    'graph_viewer',
    'megascale_stats',
]

# XPlane generated tools that support all host mode.
XPLANE_TOOLS_ALL_HOSTS_SUPPORTED = frozenset([
    'input_pipeline_analyzer',
    'framework_op_stats',
    'kernel_stats',
    'overview_page',
    'pod_viewer',
    'megascale_stats',
])

# XPlane generated tools that only support all host mode.
XPLANE_TOOLS_ALL_HOSTS_ONLY = frozenset(
    ['overview_page', 'pod_viewer'])

# Rate limiter constants, the GCS quota defined below
# https://cloud.google.com/storage/quotas#rate-quotas.
# currently set to 1000 request per minute.
# TODO(kcai): The assumption on the average number of subdirs is not
# always true. If this is not sufficient, we can consider a token-based
# approach that counts the number of subdirs after calling iterdir.
MAX_GCS_REQUESTS = 1000
LIMIT_WINDOW_SECONDS = 60
AVERAGE_SUBDIR_NUMBER = 10


def use_xplane(tool: str) -> bool:
  return tool in XPLANE_TOOLS


# HLO generated tools.
HLO_TOOLS = frozenset(['graph_viewer', 'memory_viewer'])


def use_hlo(tool: str) -> bool:
  return tool in HLO_TOOLS


def make_filename(host: str, tool: str) -> str:
  """Returns the name of the file containing data for the given host and tool.

  Args:
    host: Name of the host that produced the profile data, e.g., 'localhost'.
    tool: Name of the tool, e.g., 'trace_viewer'.

  Returns:
    The host name concatenated with the tool-specific extension, e.g.,
    'localhost.trace'.
  """
  filename = str(host) + '.' if host else ''
  if use_hlo(tool):
    tool = 'hlo_proto'
  elif use_xplane(tool):
    tool = 'xplane'
  return filename + TOOLS[tool]


def _parse_filename(filename: str) -> tuple[Optional[str], Optional[str]]:
  """Returns the host and tool encoded in a filename in the run directory.

  Args:
    filename: Name of a file in the run directory. The name might encode a host
      and tool, e.g., 'host.tracetable', 'host.domain.op_profile.json', or just
      a tool, e.g., 'trace', 'tensorflow_stats.pb'.

  Returns:
    A tuple (host, tool) containing the names of the host and tool, e.g.,
    ('localhost', 'trace_viewer'). Either of the tuple's components can be None.
  """
  m = _FILENAME_RE.fullmatch(filename)
  if m is None:
    return filename, None
  return m.group(1), _EXTENSION_TO_TOOL[m.group(2)]


def _get_hosts(filenames: list[str]) -> set[str]:
  """Parses a list of filenames and returns the set of hosts.

  Args:
    filenames: A list of filenames (just basenames, no directory).

  Returns:
    A set of host names encoded in the filenames.
  """
  hosts = set()
  for name in filenames:
    host, _ = _parse_filename(name)
    if host:
      hosts.add(host)
  return hosts


def _get_tools(filenames: list[str], profile_run_dir: str) -> set[str]:
  """Parses a list of filenames and returns the set of tools.

  If xplane is present in the repository, add tools that can be generated by
  xplane if we don't have a file for the tool.

  Args:
    filenames: A list of filenames.
    profile_run_dir: The run directory of the profile.

  Returns:
    A set of tool names encoded in the filenames.
  """
  tools = set()
  found = set()
  xplane_filenames = []
  for name in filenames:
    _, tool = _parse_filename(name)
    if tool == 'xplane':
      xplane_filenames.append(os.path.join(profile_run_dir, name))
      continue
    elif tool == 'hlo_proto':
      continue
    elif tool:
      tools.add(tool)
      if tool[-1] in ('@'):
        found.add(tool[:-1])
      else:
        found.add(tool)
  # profile_run_dir might be empty, like in cloud AI use case.
  if not profile_run_dir:
    if xplane_filenames:
      for item in XPLANE_TOOLS:
        if item[:-1] not in found:
          tools.add(item)
  else:
    try:
      if xplane_filenames:
        return set(convert.xspace_to_tool_names(xplane_filenames))
    except AttributeError:
      logger.warning('XPlane converters are available after Tensorflow 2.4')
  return tools


def respond(
    body: Any,
    content_type: str,
    code: int = 200,
    content_encoding: Optional[tuple[str, str]] = None,
) -> wrappers.Response:
  """Create a Werkzeug response, handling JSON serialization and CSP.

  Args:
    body: For JSON responses, a JSON-serializable object; otherwise, a raw
      `bytes` string or Unicode `str` (which will be encoded as UTF-8).
    content_type: Response content-type (`str`); use `application/json` to
      automatically serialize structures.
    code: HTTP status code (`int`).
    content_encoding: Response Content-Encoding header ('str'); e.g. 'gzip'. If
      the content type is not set, The data would be compressed and the content
      encoding would be set to gzip.

  Returns:
    A `werkzeug.wrappers.Response` object.
  """
  if content_type == 'application/json' and isinstance(
      body, (dict, list, set, tuple)):
    body = json.dumps(body, sort_keys=True)
  if not isinstance(body, bytes):
    body = body.encode('utf-8')
  csp_parts = {
      'default-src': ["'self'"],
      'script-src': [
          "'self'",
          "'unsafe-eval'",
          "'unsafe-inline'",
          'https://www.gstatic.com',
      ],
      'object-src': ["'none'"],
      'style-src': [
          "'self'",
          "'unsafe-inline'",
          'https://fonts.googleapis.com',
          'https://www.gstatic.com',
      ],
      'font-src': [
          "'self'",
          'https://fonts.googleapis.com',
          'https://fonts.gstatic.com',
          'data:',
      ],
      'connect-src': [
          "'self'",
          'data:',
          'www.gstatic.com',
      ],
      'img-src': [
          "'self'",
          'blob:',
          'data:',
      ],
      'script-src-elem': [
          "'self'",
          "'unsafe-inline'",
          # Remember to restrict on integrity when importing from jsdelivr
          # Whitelist this domain to support hlo_graph_dumper html format
          'https://cdn.jsdelivr.net/npm/',
          'https://www.gstatic.com',
      ],
  }
  csp = ';'.join((' '.join([k] + v) for (k, v) in csp_parts.items()))
  headers = [
      ('Content-Security-Policy', csp),
      ('X-Content-Type-Options', 'nosniff'),
  ]
  if content_encoding:
    headers.append(('Content-Encoding', content_encoding))
  else:
    headers.append(('Content-Encoding', 'gzip'))
    body = gzip.compress(body)
  return wrappers.Response(
      body, content_type=content_type, status=code, headers=headers
  )


def _plugin_assets(
    logdir: str, runs: list[str], plugin_name: str
) -> dict[str, list[str]]:
  result = {}
  for run in runs:
    run_path = _tb_run_directory(logdir, run)
    assets = plugin_asset_util.ListAssets(run_path, plugin_name)
    result[run] = assets
  return result


def _tb_run_directory(logdir: str, run: str) -> str:
  """Returns the TensorBoard run directory for a TensorBoard run name.

  This helper returns the TensorBoard-level run directory (the one that would)
  contain tfevents files) for a given TensorBoard run name (aka the relative
  path from the logdir root to this directory). For the root run '.' this is
  the bare logdir path; for all other runs this is the logdir joined with the
  run name.

  Args:
    logdir: the TensorBoard log directory root path
    run: the TensorBoard run name, e.g. '.' or 'train'

  Returns:
    The TensorBoard run directory path, e.g. my/logdir or my/logdir/train.
  """
  return logdir if run == '.' else os.path.join(logdir, run)


def filenames_to_hosts(filenames: list[str], tool: str) -> list[str]:
  """Convert a list of filenames to a list of host names given a tool.

  Args:
    filenames: A list of filenames.
    tool: A string representing the profiling tool.

  Returns:
    A list of hostnames.
  """
  hosts = _get_hosts(filenames)
  if len(hosts) > 1:
    if tool in XPLANE_TOOLS_ALL_HOSTS_ONLY:
      hosts = [ALL_HOSTS]
    elif tool in XPLANE_TOOLS_ALL_HOSTS_SUPPORTED:
      hosts.add(ALL_HOSTS)
  return sorted(hosts)


def validate_xplane_asset_paths(asset_paths: List[str]) -> None:
  """Validates that all xplane asset paths that are provided are valid files.

  Args:
    asset_paths: A list of asset paths.

  Raises:
    FileNotFoundError: If any of the xplane asset paths do not exist.
  """
  for asset_path in asset_paths:
    if str(asset_path).endswith(TOOLS['xplane']) and not epath.Path(
        asset_path
    ).exists():
      raise FileNotFoundError(f'Invalid asset path: {asset_path}')


class ProfilePlugin(base_plugin.TBPlugin):
  """Profile Plugin for TensorBoard."""

  plugin_name = PLUGIN_NAME

  def __init__(self, context):
    """Constructs a profiler plugin for TensorBoard.

    This plugin adds handlers for performance-related frontends.
    Args:
      context: A base_plugin.TBContext instance.
    """
    self.logdir = context.logdir
    self.data_provider = context.data_provider
    self.master_tpu_unsecure_channel = context.flags.master_tpu_unsecure_channel

    # Whether the plugin is active. This is an expensive computation, so we
    # compute this asynchronously and cache positive results indefinitely.
    self._is_active = False
    # Lock to ensure at most one thread computes _is_active at a time.
    self._is_active_lock = threading.Lock()
    # Cache to map profile run name to corresponding tensorboard dir name
    self._run_to_profile_run_dir = {}

  def is_active(self) -> bool:
    """Whether this plugin is active and has any profile data to show.

    Returns:
      Whether any run has profile data.
    """
    if not self._is_active:
      self._is_active = any(self.generate_runs())
    return self._is_active

  def get_plugin_apps(
      self,
  ) -> dict[str, Callable[[wrappers.Request], wrappers.Response]]:
    return {
        BASE_ROUTE: self.default_handler,
        INDEX_JS_ROUTE: self.static_file_route,
        INDEX_HTML_ROUTE: self.static_file_route,
        BUNDLE_JS_ROUTE: self.static_file_route,
        STYLES_CSS_ROUTE: self.static_file_route,
        MATERIALICONS_WOFF2_ROUTE: self.static_file_route,
        TRACE_VIEWER_INDEX_HTML_ROUTE: self.static_file_route,
        TRACE_VIEWER_INDEX_JS_ROUTE: self.static_file_route,
        ZONE_JS_ROUTE: self.static_file_route,
        RUNS_ROUTE: self.runs_route,
        RUN_TOOLS_ROUTE: self.run_tools_route,
        HOSTS_ROUTE: self.hosts_route,
        DATA_ROUTE: self.data_route,
        HLO_MODULE_LIST_ROUTE: self.hlo_module_list_route,
        CAPTURE_ROUTE: self.capture_route,
        LOCAL_ROUTE: self.default_handler
    }

  # pytype: disable=wrong-arg-types
  @wrappers.Request.application
  def default_handler(self, _: wrappers.Request) -> wrappers.Response:
    contents = self._read_static_file_impl('index.html')
    return respond(contents, 'text/html')

  def frontend_metadata(self):
    return base_plugin.FrontendMetadata(es_module_path='/index.js')

  def _read_static_file_impl(self, filename: str) -> bytes:
    """Reads contents from a filename.

    Args:
      filename (str): Name of the file.

    Returns:
      Contents of the file.
    Raises:
      IOError: File could not be read or found.
    """
    filepath = os.path.join(os.path.dirname(__file__), 'static', filename)

    try:
      with open(filepath, 'rb') as infile:
        contents = infile.read()
    except IOError as io_error:
      raise io_error
    return contents

  # pytype: disable=wrong-arg-types
  @wrappers.Request.application
  def static_file_route(self, request: wrappers.Request) -> wrappers.Response:
    # pytype: enable=wrong-arg-types
    filename = os.path.basename(request.path)
    extention = os.path.splitext(filename)[1]
    if extention == '.html':
      mimetype = 'text/html'
    elif extention == '.css':
      mimetype = 'text/css'
    elif extention == '.js':
      mimetype = 'application/javascript'
    else:
      mimetype = 'application/octet-stream'
    try:
      contents = self._read_static_file_impl(filename)
    except IOError:
      return respond('Fail to read the files.', 'text/plain', code=404)
    return respond(contents, mimetype)

  # pytype: disable=wrong-arg-types
  @wrappers.Request.application
  def runs_route(self, request: wrappers.Request) -> wrappers.Response:
    # pytype: enable=wrong-arg-types
    runs = self.runs_imp(request)
    return respond(runs, 'application/json')

  def runs_imp(self, request: Optional[wrappers.Request] = None) -> list[str]:
    """Returns a list all runs for the profile plugin.

    Args:
      request: Optional; werkzeug request used for grabbing ctx and experiment
        id for other host implementations
    """
    return sorted(list(self.generate_runs()), reverse=True)

  # pytype: disable=wrong-arg-types
  @wrappers.Request.application
  def run_tools_route(self, request: wrappers.Request) -> wrappers.Response:
    # pytype: enable=wrong-arg-types
    run = request.args.get('run')
    run_tools = self.run_tools_imp(run, request)
    return respond(run_tools, 'application/json')

  def run_tools_imp(
      self, run, request: Optional[wrappers.Request] = None
  ) -> list[str]:
    """Returns a list of tools given a single run.

    Args:
      run: the frontend run name, item is list returned by runs_imp
      request: Optional; werkzeug request used for grabbing ctx and experiment
        id for other host implementations
    """
    return list(self.generate_tools_of_run(run))

  def _run_host_impl(
      self, run: str, run_dir: str, tool: str
  ) -> List[HostMetadata]:
    if not run_dir:
      logger.warning('Cannot find asset directory for: %s', run)
      return []
    tool_pattern = '*.xplane.pb'
    filenames = []
    try:
      path = epath.Path(run_dir)
      filenames = path.glob(tool_pattern)
    except OSError as e:
      logger.warning('Cannot read asset directory: %s, OpError %s', run_dir, e)
    filenames = [os.fspath(os.path.basename(f)) for f in filenames]

    return [{'hostname': host} for host in filenames_to_hosts(filenames, tool)]

  def host_impl(
      self, run: str, tool: str, request: Optional[wrappers.Request] = None
  ) -> List[HostMetadata]:
    """Returns available hosts and their metadata for the run and tool in the log directory.

    In the plugin log directory, each directory contains profile data for a
    single run (identified by the directory name), and files in the run
    directory contains data for different tools and hosts. The file that
    contains profile for a specific tool "x" will have extension TOOLS["x"].

    Example:
      log/
        run1/
          plugins/
            profile/
              host1.trace
              host2.trace
              module1.hlo_proto.pb
              module2.hlo_proto.pb
        run2/
          plugins/
            profile/
              host1.trace
              host2.trace

    Args:
      run: the frontend run name, e.g., 'run1' or 'run2' for the example above.
      tool: the requested tool, e.g., 'trace_viewer' for the example above.
      request: Optional; werkzeug request used for grabbing ctx and experiment
        id for other host implementations

    Returns:
      A list of host names, e.g.:
        host_impl(run1, trace_viewer) --> [{"hostname": "host1"}, {"hostname":
        "host2"}]
        host_impl(run1, memory_viewer) --> [{"hostname": "module1"},
        {"hostname":
        "module2"}]
    """

    run_dir = self._run_dir(run)
    return self._run_host_impl(run, run_dir, tool)

  # pytype: disable=wrong-arg-types
  @wrappers.Request.application
  def hosts_route(self, request: wrappers.Request) -> wrappers.Response:
    # pytype: enable=wrong-arg-types
    run = request.args.get('run')
    tool = request.args.get('tag')
    hosts = self.host_impl(run, tool, request)
    return respond(hosts, 'application/json')

  # pytype: disable=wrong-arg-types
  @wrappers.Request.application
  def hlo_module_list_route(
      self, request: wrappers.Request
  ) -> wrappers.Response:
    module_names_str = self.hlo_module_list_impl(request)
    return respond(module_names_str, 'text/plain')

  def data_impl(
      self, request: wrappers.Request
  ) -> tuple[Optional[str], str, Optional[str]]:
    """Retrieves and processes the tool data for a run and a host.

    Args:
      request: XMLHttpRequest

    Returns:
      A string that can be served to the frontend tool or None if tool,
        run or host is invalid.
    """
    run = request.args.get('run')
    tool = request.args.get('tag')
    host = request.args.get('host')
    module_name = request.args.get('module_name')
    tqx = request.args.get('tqx')
    use_saved_result_str = request.args.get('use_saved_result', 'true')
    use_saved_result = use_saved_result_str.lower() != 'false'
    run_dir = self._run_dir(run)

    # Check if the cache file exists and if the version is the same as the
    # current version. If not, set use_saved_result to False.
    try:
      if epath.Path(os.path.join(run_dir, CACHE_VERSION_FILE)).exists():
        with epath.Path(os.path.join(run_dir, CACHE_VERSION_FILE)).open(
            'r'
        ) as f:
          cache_version = f.read().strip()
          if cache_version != version.__version__:
            use_saved_result = False
      else:
        use_saved_result = False
    except OSError as e:
      logger.warning('Cannot read cache version file: %s', e)
      use_saved_result = False

    graph_viewer_options = self._get_graph_viewer_options(request)
    # Host param is used by HLO tools to identify the module.
    params = {
        'graph_viewer_options': graph_viewer_options,
        'tqx': tqx,
        'host': host,
        'module_name': module_name,
        'use_saved_result': use_saved_result,
    }
    content_type = 'application/json'

    if tool not in TOOLS and not use_xplane(tool):
      return None, content_type, None
    if tool == 'memory_viewer' and request.args.get(
        'view_memory_allocation_timeline'
    ):
      params['view_memory_allocation_timeline'] = True

    params['memory_space'] = request.args.get('memory_space', '0')

    if tool == 'trace_viewer@':
      options = {}
      options['resolution'] = request.args.get('resolution', 8000)
      if request.args.get('start_time_ms') is not None:
        options['start_time_ms'] = request.args.get('start_time_ms')
      if request.args.get('end_time_ms') is not None:
        options['end_time_ms'] = request.args.get('end_time_ms')
      params['trace_viewer_options'] = options

    asset_path = os.path.join(run_dir, make_filename(host, tool))

    _, content_encoding = None, None
    if use_xplane(tool):
      if host == ALL_HOSTS:
        file_pattern = make_filename('*', 'xplane')
        try:
          path = epath.Path(run_dir)
          asset_paths = list(path.glob(file_pattern))
        except OSError as e:
          logger.warning('Cannot read asset directory: %s, OpError %s', run_dir,
                         e)
          raise IOError(
              'Cannot read asset directory: %s, OpError %s' % (run_dir, e)
          ) from e
      else:
        asset_paths = [asset_path]

      try:
        validate_xplane_asset_paths(asset_paths)
        data, content_type = convert.xspace_to_tool_data(
            asset_paths, tool, params)
      except AttributeError as e:
        logger.warning('Error generating analysis results due to %s', e)
        raise AttributeError(
            'Error generating analysis results due to %s' % e
        ) from e
      except ValueError as e:
        logger.warning('XPlane convert to tool data failed as %s', e)
        raise e
      except FileNotFoundError as e:
        logger.warning('XPlane convert to tool data failed as %s', e)
        raise e

      # Write cache version file if use_saved_result is False.
      if not use_saved_result:
        try:
          with epath.Path(os.path.join(run_dir, CACHE_VERSION_FILE)).open(
              'w'
          ) as f:
            f.write(version.__version__)
        except OSError as e:
          logger.warning('Cannot write cache version file: %s', e)

      return data, content_type, content_encoding

    logger.info('%s does not use xplane', tool)
    return None, content_type, None

  def hlo_module_list_impl(
      self, request: wrappers.Request
  ) -> str:
    """Returns a string of HLO module names concatenated by comma for the given run."""
    run = request.args.get('run')
    run_dir = self._run_dir(run)
    module_list = []
    if not run_dir:
      logger.warning('Cannot find asset directory for: %s', run)
      return ''
    tool_pattern = '*.hlo_proto.pb'
    filenames = []
    try:
      path = epath.Path(run_dir)
      filenames = path.glob(tool_pattern)
    except OSError as e:
      logger.warning('Cannot read asset directory: %s, OpError %s', run_dir, e)
    filenames = [os.fspath(os.path.basename(f)) for f in filenames]
    for filename in filenames:
      module_name, _ = _parse_filename(filename)
      if module_name:
        module_list.append(module_name)
    module_names_str = ','.join(module_list)
    return module_names_str

  # pytype: disable=wrong-arg-types
  @wrappers.Request.application
  def data_route(self, request: wrappers.Request) -> wrappers.Response:
    # pytype: enable=wrong-arg-types
    # params
    #   request: XMLHTTPRequest.
    try:
      data, content_type, content_encoding = self.data_impl(request)
      if data is None:
        return respond('No Data', 'text/plain', code=404)
      return respond(data, content_type, content_encoding=content_encoding)
    # Data fetch error handler
    except TimeoutError as e:
      return respond(str(e), 'text/plain', code=500)
    except AttributeError as e:
      return respond(str(e), 'text/plain', code=500)
    except ValueError as e:
      return respond(str(e), 'text/plain', code=500)
    except FileNotFoundError as e:
      return respond(str(e), 'text/plain', code=500)
    except IOError as e:
      return respond(str(e), 'text/plain', code=500)

  # pytype: disable=wrong-arg-types
  @wrappers.Request.application
  def capture_route(self, request: wrappers.Request) -> wrappers.Response:
    # pytype: enable=wrong-arg-types
    return self.capture_route_impl(request)

  def capture_route_impl(self, request: wrappers.Request) -> wrappers.Response:
    """Runs the client trace for capturing profiling information."""

    if not tf or not profiler or not profiler_client:
      return respond(
          {'error': 'TensorFlow is not installed.'},
          'application/json',
          code=500,
      )

    def get_worker_list(
        cluster_resolver: tf.distribute.cluster_resolver.ClusterResolver,
    ) -> str:
      """Parses TPU workers list from the cluster resolver."""
      cluster_spec = cluster_resolver.cluster_spec()
      task_indices = cluster_spec.task_indices('worker')
      worker_list = [
          cluster_spec.task_address('worker', i).replace(':8470', ':8466')
          for i in task_indices
      ]
      return ','.join(worker_list)

    service_addr = request.args.get('service_addr')
    duration = int(request.args.get('duration', '1000'))
    is_tpu_name = request.args.get('is_tpu_name') == 'true'
    worker_list = request.args.get('worker_list')
    num_tracing_attempts = int(request.args.get('num_retry', '0')) + 1
    options = None
    try:
      options = profiler.ProfilerOptions(
          host_tracer_level=int(request.args.get('host_tracer_level', '2')),
          device_tracer_level=int(request.args.get('device_tracer_level', '1')),
          python_tracer_level=int(request.args.get('python_tracer_level', '0')),
      )
      # For preserving backwards compatibility with TensorFlow 2.3 and older.
      if 'delay_ms' in options._fields:
        options.delay_ms = int(request.args.get('delay', '0'))
    except AttributeError:
      logger.warning('ProfilerOptions are available after tensorflow 2.3')

    if is_tpu_name:
      try:
        tpu_cluster_resolver = (
            tf.distribute.cluster_resolver.TPUClusterResolver(service_addr)
        )
        master_grpc_addr = tpu_cluster_resolver.get_master()
      except (ImportError, RuntimeError) as err:
        return respond({'error': repr(err)}, 'application/json', code=500)
      except (ValueError, TypeError):
        return respond(
            {'error': 'no TPUs with the specified names exist.'},
            'application/json',
            code=500,
        )
      if not worker_list:
        worker_list = get_worker_list(tpu_cluster_resolver)
      # TPU cluster resolver always returns port 8470. Replace it with 8466
      # on which profiler service is running.
      master_ip = master_grpc_addr.replace('grpc://', '').replace(':8470', '')
      service_addr = master_ip + ':8466'
      # Set the master TPU for streaming trace viewer.
      self.master_tpu_unsecure_channel = master_ip
    try:
      if options:
        profiler_client.trace(
            service_addr,
            self.logdir,
            duration,
            worker_list,
            num_tracing_attempts,
            options=options)
      else:
        profiler_client.trace(
            service_addr,
            self.logdir,
            duration,
            worker_list,
            num_tracing_attempts,
        )
      return respond(
          {'result': 'Capture profile successfully. Please refresh.'},
          'application/json',
      )
    except tf.errors.UnavailableError:
      return respond(
          {'error': 'empty trace result.'},
          'application/json',
          code=200,
      )
    except Exception as e:  # pylint: disable=broad-except
      return respond(
          {'error': str(e)},
          'application/json',
          code=500,
      )

  def _get_graph_viewer_options(
      self, request: wrappers.Request
  ) -> dict[str, Any]:
    node_name = request.args.get('node_name')
    module_name = request.args.get('module_name')
    graph_width_str = request.args.get('graph_width') or ''
    graph_width = int(graph_width_str) if graph_width_str.isdigit() else 3
    show_metadata = int(request.args.get('show_metadata') == 'true')
    merge_fusion = int(request.args.get('merge_fusion') == 'true')
    return {
        'node_name': node_name,
        'module_name': module_name,
        'graph_width': graph_width,
        'show_metadata': show_metadata,
        'merge_fusion': merge_fusion,
        'format': request.args.get('format'),
        'type': request.args.get('type')
    }

  def _run_dir(self, run: str) -> str:
    """Helper that maps a frontend run name to a profile "run" directory.

    The frontend run name consists of the TensorBoard run name (aka the relative
    path from the logdir root to the directory containing the data) path-joined
    to the Profile plugin's "run" concept (which is a subdirectory of the
    plugins/profile directory representing an individual run of the tool), with
    the special case that TensorBoard run is the logdir root (which is the run
    named '.') then only the Profile plugin "run" name is used, for backwards
    compatibility.

    Args:
      run: the frontend run name, as described above, e.g. train/run1.

    Returns:
      The resolved directory path, e.g. /logdir/train/plugins/profile/run1.

    Raises:
      RuntimeError: If the run directory is not found.
    """
    run = run.rstrip(os.sep)
    tb_run_name, profile_run_name = os.path.split(run)
    if not tb_run_name:
      tb_run_name = '.'
    tb_run_directory = _tb_run_directory(self.logdir, tb_run_name)
    if not epath.Path(tb_run_directory).is_dir():
      raise RuntimeError('No matching run directory for run %s' % run)

    plugin_directory = plugin_asset_util.PluginDirectory(
        tb_run_directory, PLUGIN_NAME)
    return os.path.join(plugin_directory, profile_run_name)

  def generate_runs(self) -> Iterator[str]:
    """Generator for a list of runs.

    The "run name" here is a "frontend run name" - see _run_dir() for the
    definition of a "frontend run name" and how it maps to a directory of
    profile data for a specific profile "run". The profile plugin concept of
    "run" is different from the normal TensorBoard run; each run in this case
    represents a single instance of profile data collection, more similar to a
    "step" of data in typical TensorBoard semantics. These runs reside in
    subdirectories of the plugins/profile directory within any regular
    TensorBoard run directory (defined as a subdirectory of the logdir that
    contains at least one tfevents file) or within the logdir root directory
    itself (even if it contains no tfevents file and would thus not be
    considered a normal TensorBoard run, for backwards compatibility).

    `generate_runs` will get all runs first, and get tools list from
    `generate_tools_of_run` for a single run due to expensive processing for
    xspace data to parse the tools.
    Example:
      logs/
        plugins/
          profile/
            run1/
              hostA.trace
        train/
          events.out.tfevents.foo
          plugins/
            profile/
              run1/
                hostA.trace
                hostB.trace
              run2/
                hostA.trace
        validation/
          events.out.tfevents.foo
          plugins/
            profile/
              run1/
                hostA.trace
        new_job/
          tensorboard/
            plugins/
              profile/
                run1/
                  hostA.xplane.pb
    Yields:
    A sequence of string that are "frontend run names".
    For the above example, this would be:
        "run1", "train/run1", "train/run2", "validation/run1",
        "new_job/tensorboard/run1"
    """

    # TODO(kcai): Remove this block once we can rely on walk() to get all
    #             subdirectories, this requires python 3.12.
    def find_all_subdirectories(top_path: epath.Path) -> Iterator[epath.Path]:
      @sleep_and_retry
      @limits(
          calls=MAX_GCS_REQUESTS / AVERAGE_SUBDIR_NUMBER,
          period=LIMIT_WINDOW_SECONDS,
      )
      def get_subdirectories(
          current_dir: epath.Path, dirs_to_visit: collections.deque[epath.Path]
      ):
        try:
          for path in current_dir.iterdir():
            if path.is_dir():
              dirs_to_visit.append(path)
        except (IOError, OSError) as e:
          logger.warning('Could not list directory %s: %s', current_dir, e)

      if not top_path.is_dir():
        return

      dirs_to_visit = collections.deque([top_path])

      logger.info(
          'Start to find all subdirectories of %s at %s, subjected to be'
          ' throttled by %d requests per %d seconds',
          top_path,
          time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
          MAX_GCS_REQUESTS,
          LIMIT_WINDOW_SECONDS,
      )
      while dirs_to_visit:
        current_dir = dirs_to_visit.popleft()
        yield current_dir
        get_subdirectories(current_dir, dirs_to_visit)
      logger.info(
          'Finish finding all subdirectories of %s at %s',
          top_path,
          time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
      )

    ctx = tb_context.RequestContext()
    tb_runs = set()
    # Get all tfevents files that TensorBoard would consider runs.
    # TODO(kcai): Remove this block once we can rely on the behavior of
    #             list_runs() returning all subdirectories with tfevents files.
    for run in self.data_provider.list_runs(ctx, experiment_id=''):
      # Ensure that we also check the parent directory of runs generated by
      # Tensorboard.
      # Example:
      # logs/
      #   2024-08-20-12-34-56/
      #     plugins/profile/run1/hostA.trace
      #     train/events.out.tfevents.foo
      #     validation/events.out.tfevents.foo
      # list_runs() will return:
      #   2024-08-20-12-34-56/train
      #   2024-08-20-12-34-56/validation
      # and we want to ensure that we also check the parent directory:
      #   2024-08-20-12-34-56/
      if os.path.basename(run.run_name) in ['train', 'validation']:
        tb_runs.add(os.path.dirname(run.run_name))
    # Ensure that we also check the root logdir and all subdirectories, even if
    # it isn't a recognized TensorBoard run (i.e. has no tfevents file directly
    # under it), to remain backwards compatible with previously profile plugin
    # behavior. Note that we check if logdir is a directory to handle case where
    # it's actually a multipart directory spec, which this plugin does not
    # support.
    #
    # This change still enforce the requirement that the subdirectories must
    # end with plugins/profile directory, as enforced by TensorBoard.
    logdir_path = epath.Path(self.logdir)
    if '.' not in tb_runs:
      tb_runs.add('.')
    if logdir_path.is_dir():
      for path in find_all_subdirectories(logdir_path):
        relative_path = path.relative_to(logdir_path)
        try:
          *parts, second_last_dir, last_dir = relative_path.parts
          # Only add subdirectories to runs that are end with plugins/profile.
          if (
              len(parts) >= 1  # len(parts) == 0 is the root logdir.
              and last_dir == PLUGIN_NAME
              and second_last_dir == TB_NAME
          ):
            tb_runs.add(str(epath.Path(*parts)))
        except ValueError:
          logger.info('Could not unpack relative path parts: %s', relative_path)
          pass
    tb_run_names_to_dirs = {
        run: _tb_run_directory(self.logdir, run) for run in tb_runs
    }
    plugin_assets = _plugin_assets(
        self.logdir, list(tb_run_names_to_dirs), PLUGIN_NAME
    )
    visited_runs = set()
    for tb_run_name, profile_runs in six.iteritems(plugin_assets):
      tb_run_dir = tb_run_names_to_dirs[tb_run_name]
      tb_plugin_dir = plugin_asset_util.PluginDirectory(tb_run_dir, PLUGIN_NAME)

      for profile_run in profile_runs:
        # Remove trailing separator; some filesystem implementations emit this.
        profile_run = profile_run.rstrip(os.sep)
        if tb_run_name == '.':
          frontend_run = profile_run
        else:
          frontend_run = os.path.join(tb_run_name, profile_run)
        profile_run_dir = os.path.join(tb_plugin_dir, profile_run)
        if epath.Path(profile_run_dir).is_dir():
          self._run_to_profile_run_dir[frontend_run] = profile_run_dir
          if frontend_run not in visited_runs:
            visited_runs.add(frontend_run)
            yield frontend_run

  def generate_tools_of_run(self, run: str) -> Iterator[str]:
    """Generate a list of tools given a certain run."""
    profile_run_dir = self._run_to_profile_run_dir[run]
    if epath.Path(profile_run_dir).is_dir():
      try:
        filenames = epath.Path(profile_run_dir).iterdir()
      except OSError as e:
        logger.warning('Cannot read asset directory: %s, NotFoundError %s',
                       profile_run_dir, e)
        filenames = []
      if filenames:
        for tool in self._get_active_tools(
            [name.name for name in filenames], profile_run_dir
        ):
          yield tool

  def _get_active_tools(self, filenames, profile_run_dir=''):
    """Get a list of tools available given the filenames created by profiler.

    Args:
      filenames: List of strings that represent filenames
      profile_run_dir: The run directory of the profile.

    Returns:
      A list of strings representing the available tools
    """
    tools = _get_tools(filenames, profile_run_dir)
    if 'trace_viewer@' in tools:
      # streaming trace viewer always override normal trace viewer.
      # the trailing '@' is to inform tf-profile-dashboard.html and
      # tf-trace-viewer.html that stream trace viewer should be used.
      tools.discard('trace_viewer')
    # Return sorted list of tools with 'overview_page' at the front.
    op = frozenset(['overview_page'])
    return list(tools.intersection(op)) + sorted(tools.difference(op))
