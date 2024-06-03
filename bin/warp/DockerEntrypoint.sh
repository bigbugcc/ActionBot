#!/bin/sh

config_file="/etc/warp/config.json"
example_config_file="/app/example_config.json"


# Check if the config file exists
if [ ! -f "$config_file" ]; then
    # Copy the example config file to the warp directory
    cp "$example_config_file" "$config_file"
fi

# Start the warp-plus executable
exec /usr/bin/warp-plus -c $config_file