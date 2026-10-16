#!/usr/bin/env bash
# Build pywhispercpp (whisper.cpp) with the Vulkan backend and install it into
# this project's venv (not a declared dependency, so `uv run` leaves it alone; `uv sync`
# removes it, then rerun with --install). No sudo needed:
# the Vulkan headers and shader compiler are unpacked from Ubuntu packages.
#
#   scripts/build-whispercpp-vulkan.sh            # build (once, ~5 min) and install
#   scripts/build-whispercpp-vulkan.sh --install  # reinstall the built wheel (after `uv sync`)
#   scripts/build-whispercpp-vulkan.sh --wheel    # only build the wheel (scripts/install.sh uses it)
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
version="1.5.1"
work="${XDG_CACHE_HOME:-$HOME/.cache}/tesla/whispercpp-vulkan"
wheels="${XDG_DATA_HOME:-$HOME/.local/share}/tesla/wheels"
mkdir -p "$work" "$wheels"

if [[ "${1:-}" != "--install" ]]; then
  # Vulkan headers, loader symlink and glslc from Ubuntu packages, unpacked locally.
  mkdir -p "$work/debs" "$work/sysroot"
  (cd "$work/debs" && apt-get download glslc libshaderc1 libvulkan-dev spirv-headers >/dev/null)
  for deb in "$work"/debs/*.deb; do dpkg -x "$deb" "$work/sysroot"; done
  libdir="$work/sysroot/usr/lib/x86_64-linux-gnu"
  ln -sf /usr/lib/x86_64-linux-gnu/libvulkan.so.1 "$libdir/libvulkan.so"

  [[ -d "$work/pywhispercpp" ]] || git clone -q --depth 1 --branch "v$version" --recursive \
    --shallow-submodules https://github.com/absadiki/pywhispercpp "$work/pywhispercpp"

  uv venv -q --allow-existing --python 3.12 "$work/venv"
  uv pip install -q --python "$work/venv/bin/python" pip cmake ninja setuptools wheel pybind11 numpy
  rm -f "$wheels"/pywhispercpp-*.whl
  # pywhispercpp's setup.py passes every environment variable to CMake as -D...,
  # so run it with a minimal environment.
  env -i HOME="$HOME" PATH="$work/venv/bin:$work/sysroot/usr/bin:/usr/bin:/bin" \
    LD_LIBRARY_PATH="$libdir" CMAKE_BUILD_PARALLEL_LEVEL="$(nproc)" GGML_VULKAN=1 PYWHISPERCPP_VERSION="$version" \
    CMAKE_ARGS="-DVulkan_INCLUDE_DIR=$work/sysroot/usr/include -DVulkan_LIBRARY=$libdir/libvulkan.so -DVulkan_GLSLC_EXECUTABLE=$work/sysroot/usr/bin/glslc" \
    "$work/venv/bin/python" -m pip wheel --no-build-isolation --no-deps -w "$wheels" "$work/pywhispercpp"
fi

[[ "${1:-}" == "--wheel" ]] && { ls "$wheels"/pywhispercpp-*.whl; exit 0; }
wheel="$(ls "$wheels"/pywhispercpp-*.whl)"
(cd "$root" && uv pip install -q --reinstall-package pywhispercpp "$wheel")
echo "Installed $wheel"
(cd "$root" && uv run python -c "from tesla.stt import vulkan_available; print('Vulkan:', vulkan_available())")
