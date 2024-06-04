#!/bin/bash
# Modify Dockerfile Timezone
sed -i '/ENV TZ=/c\ENV TZ=Asia/Shanghai' Dockerfile