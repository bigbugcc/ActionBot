#!/bin/bash
# Modify Dockerfile Timezone
sed -i '/ENV TZ=/c\ENV TZ=Asia/Shanghai' Dockerfile

# Patch
sed -i '/FROM golang/c\FROM golang:1.23-alpine AS builder' Dockerfile