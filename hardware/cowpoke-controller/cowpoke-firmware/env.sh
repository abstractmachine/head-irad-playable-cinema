# Run "source env.sh" while the Terminal is at the workspace root.
export COWPOKE_ROOT="$PWD"
export PICO_SDK_PATH="$COWPOKE_ROOT/pico-sdk"
export PICO_TOOLCHAIN_PATH="$COWPOKE_ROOT/toolchains/arm-gnu-toolchain-14.2.rel1-darwin-arm64-arm-none-eabi"
export PATH="$COWPOKE_ROOT/toolchains/build-tools/bin:$PICO_TOOLCHAIN_PATH/bin:/opt/homebrew/bin:$PATH"
export GP2040_BOARDCONFIG=Pico
export PICO_BOARD=pico
export PIP_CONSTRAINT="$COWPOKE_ROOT/python-constraints.txt"