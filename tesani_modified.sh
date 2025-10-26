#!/bin/bash

# Fixed Tisane API script using correct API format

TRANSCRIPT="$*"
LOG_FILE="/home/pi/sr/tisane_debug.log"
API_KEY=$(curl -s http://SERVER_IP:port/get-keys | jq -r '.tisane')

# Function to log messages
log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Check if transcript is provided
if [ -z "$TRANSCRIPT" ]; then
    log_message "ERROR: No transcript provided"
    echo '{"error": "No transcript provided", "abuse": []}'
    exit 1
fi

# Log the request
log_message "Processing transcript: '$TRANSCRIPT'"

# Make the API call with correct format based on documentation
response=$(curl --silent --show-error --location 'https://api.tisane.ai/parse' \
    --max-time 10 \
    --retry 2 \
    --header 'Content-Type: application/json' \
    --header "Ocp-Apim-Subscription-Key: $API_KEY" \
    --data "{
        \"language\": \"en\",
        \"content\": \"$TRANSCRIPT\",
        \"settings\": {
            \"abuse\": true,
            \"snippets\": true,
            \"explain\": false
        }
    }" 2>&1)

# Check curl exit status
curl_exit_code=$?
if [ $curl_exit_code -ne 0 ]; then
    log_message "ERROR: Curl failed with exit code $curl_exit_code"
    log_message "ERROR: Response: $response"
    echo '{"error": "API request failed", "abuse": []}'
    exit 1
fi

# Log the raw response
log_message "Raw API response: $response"

# Validate JSON response
if ! echo "$response" | jq empty 2>/dev/null; then
    log_message "ERROR: Invalid JSON response"
    echo '{"error": "Invalid JSON response", "abuse": []}'
    exit 1
fi

# Check if response contains an error
if echo "$response" | jq -e '.error' >/dev/null 2>&1; then
    log_message "ERROR: API returned error: $(echo "$response" | jq -r '.error')"
    echo "$response"
    exit 1
fi

# Process the response - extract the abuse section
abuse_section=$(echo "$response" | jq '.abuse // []' 2>/dev/null)
if [ $? -eq 0 ]; then
    log_message "Processed response with $(echo "$abuse_section" | jq 'length') abuse instances"
    echo "{\"abuse\": $abuse_section}"
else
    log_message "Failed to extract abuse section"
    echo '{"error": "Failed to process response", "abuse": []}'
fi
