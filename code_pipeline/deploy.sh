#!/bin/bash

set -e

# Deploys the Course Creator multi-agent application to Google Cloud Run.
#
# Parameters:
#   --no-redeploy: (Optional) If set, services that are already deployed and have a URL will not be redeployed.
#   --revision-tag: (Optional) A specific revision tag to apply to the deployment.

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd "${SCRIPT_DIR}"

################## Initialization ##################

# Parse script arguments
NO_REDEPLOY="false" # Redeploying all services by default.
while [[ $# -gt 0 ]]; do
  case $1 in
    --no-redeploy)  NO_REDEPLOY="true"; shift ;;
    --revision-tag) REVISION_TAG="$2"; shift 2 ;;
    *) shift ;; # Ignore unknown flags
  esac
done

# Load .env file if it exists.
# Optionally, use a custom .env file path via ENV_FILE environment variable.
if [[ "$ENV_FILE" == "" ]]; then
    export ENV_FILE=".env"
fi
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
elif [[ -f "$HOME/$ENV_FILE" ]]; then
    read -r -p "⚠️ WARNING: $ENV_FILE not found in the project directory. Use '$HOME/$ENV_FILE' instead? [y/N]> " response
    if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        echo "Using '$HOME/$ENV_FILE' file."
        source "$HOME/$ENV_FILE"
    else
        exit 1
    fi
elif [[ "${BUILD_ID}" == "" ]]; then
    # Warn the user that the .env file is not found, unless we are running in a Cloud Build pipeline.
    echo "⚠️ WARNING: $ENV_FILE file not found. Using current or default values."
fi

# If GOOGLE_CLOUD_PROJECT is not defined, get current project from gcloud CLI
if [[ "${GOOGLE_CLOUD_PROJECT}" == "" ]]; then
    GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project -q)
fi
if [[ "${GOOGLE_CLOUD_PROJECT}" == "" ]]; then
    echo "ERROR: Run 'gcloud config set project' command to set active project, or set GOOGLE_CLOUD_PROJECT environment variable."
    exit 1
fi

# GOOGLE_CLOUD_REGION is the region where Cloud Run services will be deployed.
# GOOGLE_CLOUD_LOCATION is a cloud location used for Gemini API calls, it may be a region, and may be "global".
# If GOOGLE_CLOUD_REGION is not defined, it will be the same as GOOGLE_CLOUD_LOCATION unless GOOGLE_CLOUD_LOCATION is "global".
# In that case, the region will be assigned to the default Cloud Run region configured with gcloud CLI.
# If none is configured, "us-central1" is the default value.
if [[ "${GOOGLE_CLOUD_REGION}" == "" ]]; then
    GOOGLE_CLOUD_REGION="${GOOGLE_CLOUD_LOCATION}"
fi
if [[ "${GOOGLE_CLOUD_REGION}" == "global" ]]; then
    echo "GOOGLE_CLOUD_REGION is set to 'global'. Getting a default location for Cloud Run."
    GOOGLE_CLOUD_REGION=""
fi
if [[ "${GOOGLE_CLOUD_REGION}" == "" ]]; then
    GOOGLE_CLOUD_REGION=$(gcloud config get-value run/region -q)
    if [[ "${GOOGLE_CLOUD_REGION}" == "" ]]; then
        GOOGLE_CLOUD_REGION="us-central1"
        echo "WARNING: Cannot get a configured Cloud Run region. Defaulting to ${GOOGLE_CLOUD_REGION}."
    fi
fi
# If GOOGLE_CLOUD_LOCATION is empty, "global" will be used.
if [[ "${GOOGLE_CLOUD_LOCATION}" == "" ]]; then
    GOOGLE_CLOUD_LOCATION="global"
fi

echo "Using project ${GOOGLE_CLOUD_PROJECT}."
echo "Using Cloud Run region ${GOOGLE_CLOUD_REGION}."
echo "Using Gemini/ADK location ${GOOGLE_CLOUD_LOCATION}."

if [[ "${GOOGLE_CLOUD_LOCATION}" != "global" ]]; then
    echo "⚠️ WARNING: Location for Gemini/ADK is not 'global'. This may cause issues with Gemini 3 API calls."
fi

export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT}"
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION}"
export GOOGLE_CLOUD_REGION="${GOOGLE_CLOUD_REGION}"

################## FUNCTIONS ##################

get_service_url() {
    # Retrieves the url of a Cloud Run service.
    # Parameters:
    #   1. Service Name - name of the service.
    #   2. [Optional] Service Revision tag if not active (serving traffic) revision is needed.
    SERVICE_NAME=$1
    REVISION_TAG_NAME=$2
    SERVICE_URL=$(gcloud run services describe $SERVICE_NAME --region $GOOGLE_CLOUD_REGION --project $GOOGLE_CLOUD_PROJECT --format='value(status.url)' 2>/dev/null || echo "")
    if [[ "${SERVICE_URL}" == "" ]]; then
        # No serving service deployment found.
        echo ""
        return 0
    fi
    if [[ "${REVISION_TAG_NAME}" != "" ]]; then
        HAS_REVISON=$(gcloud run services describe $SERVICE_NAME --region $GOOGLE_CLOUD_REGION --project $GOOGLE_CLOUD_PROJECT --format="value(status.traffic.filter(tag='$REVISION_TAG_NAME'))" 2>/dev/null || echo "")
        if [[ "${HAS_REVISON}" == "" ]]; then
            echo ""
            return 0
        fi
        REVISION_TAG_URL_PREFIX="${REVISION_TAG_NAME}---"
        SERVICE_URL="${SERVICE_URL/https:\/\//https://$REVISION_TAG_URL_PREFIX}" # optionally, insert "{tag}---" after "https://"
    fi
    echo $SERVICE_URL
}


deploy_service() {
    # Deploys a Cloud Run service.
    # Parameters:
    #   1. SERVICE_NAME - Name of the service.
    #   2. SOURCE_DIR - Directory containing the source code.
    #   3. ADD_PARAMS - (Optional) Additional gcloud parameters. NOTE: This parameter is not allowed to have newline at the beginning or end of the passed value.
    #   4. REVISION_TAG_NAME - (Optional) Revision tag to apply.
    SERVICE_NAME="$1"
    SOURCE_DIR="$2"
    ADD_PARAMS="$3"
    REVISION_TAG_NAME="$4"

    if [[ "${REVISION_TAG_NAME}" != "" ]]; then
        SERVING_URL=$(get_service_url $SERVICE_NAME 2>/dev/null || echo "")
        # If no existing serving deployment, we cannot use "--no-traffic"
        if [[ "${SERVING_URL}" != "" ]]; then
            TAG_PARAMS=" --no-traffic --tag ${REVISION_TAG_NAME} --set-env-vars REVISION_TAG=${REVISION_TAG_NAME} "
        else
            TAG_PARAMS=" --tag ${REVISION_TAG_NAME} --set-env-vars REVISION_TAG=${REVISION_TAG_NAME} "
        fi
        SWITCH_TO_CURRENT="false"
    else
        SWITCH_TO_CURRENT="true"
        REVISION_TAG_NAME="r-$RANDOM-$RANDOM"
        TAG_PARAMS=" --tag ${REVISION_TAG_NAME} --set-env-vars REVISION_TAG=${REVISION_TAG_NAME} "
    fi

    echo "Deploying ${SERVICE_NAME}..."



    gcloud run deploy $SERVICE_NAME \
        --source "${SOURCE_DIR}" \
        --project $GOOGLE_CLOUD_PROJECT \
        --region $GOOGLE_CLOUD_REGION $TAG_PARAMS \
        --no-allow-unauthenticated $ADD_PARAMS \
        --set-env-vars GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT}" \
        --set-env-vars GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION}" \
        --set-env-vars GOOGLE_GENAI_USE_VERTEXAI="true" \
        --set-env-vars OTEL_SERVICE_NAME="${SERVICE_NAME}" \
        --set-env-vars ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS="false" \
        --set-env-vars OTEL_TRACES_SAMPLER="always_on" \
        --labels dev-tutorial=prod-ready-2
    if [[ "${SWITCH_TO_CURRENT}" == "true" ]]; then
        gcloud run services update-traffic $SERVICE_NAME --to-tags ${REVISION_TAG_NAME}=100 --project $GOOGLE_CLOUD_PROJECT --region $GOOGLE_CLOUD_REGION
    fi
}

################## Main Script ##################

# Enable required Google Cloud APIs.
echo "📦 Enabling required Google Cloud APIs..."
gcloud services enable \
    aiplatform.googleapis.com \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    serviceusage.googleapis.com \
    monitoring.googleapis.com \
    logging.googleapis.com \
    cloudtrace.googleapis.com \
    --project="${GOOGLE_CLOUD_PROJECT}"

echo "Checking on existing deployments..."
if [[ "${NO_REDEPLOY}" == "true" ]]; then
    export PLANNER_URL=$(get_service_url "planner" $REVISION_TAG 2>/dev/null || echo "")
    export PATCHER_URL=$(get_service_url "patcher" $REVISION_TAG 2>/dev/null || echo "")
    export CRITIC_URL=$(get_service_url "critic" $REVISION_TAG 2>/dev/null || echo "")
    export CODE_ORCHESTRATOR_URL=$(get_service_url "code-orchestrator" $REVISION_TAG 2>/dev/null || echo "")
    export APP_URL=$(get_service_url "code-pipeline-app" $REVISION_TAG 2>/dev/null || echo "")
fi

if [[ "${PLANNER_URL}" == "" ]]; then
    deploy_service planner agents/planner "" $REVISION_TAG
    export PLANNER_URL=$(get_service_url "planner" $REVISION_TAG)
fi

if [[ "${CRITIC_URL}" == "" ]]; then
    deploy_service critic agents/critic "" $REVISION_TAG
    export CRITIC_URL=$(get_service_url "critic" $REVISION_TAG)
fi

if [[ "${PATCHER_URL}" == "" ]]; then
    deploy_service patcher agents/patcher "" $REVISION_TAG
    export PATCHER_URL=$(get_service_url "patcher" $REVISION_TAG)
fi

if [[ "${CODE_ORCHESTRATOR_URL}" == "" ]]; then
    ADD_VARS="--set-env-vars PLANNER_AGENT_CARD_URL=$PLANNER_URL/a2a/agent/.well-known/agent-card.json \
        --set-env-vars CRITIC_AGENT_CARD_URL=$CRITIC_URL/a2a/agent/.well-known/agent-card.json \
        --set-env-vars PATCHER_AGENT_CARD_URL=$PATCHER_URL/a2a/agent/.well-known/agent-card.json"
    deploy_service code-orchestrator agents/code_orchestrator "$ADD_VARS" $REVISION_TAG
    export CODE_ORCHESTRATOR_URL=$(get_service_url "code-orchestrator" $REVISION_TAG)
fi

if [[ "${APP_URL}" == "" ]]; then
    ADD_VARS="--set-env-vars AGENT_SERVER_URL=$CODE_ORCHESTRATOR_URL"
    deploy_service code-pipeline-app app "$ADD_VARS" $REVISION_TAG

    gcloud run services update code-pipeline-app \
        --project $GOOGLE_CLOUD_PROJECT \
        --region $GOOGLE_CLOUD_REGION \
        --no-invoker-iam-check

    export APP_URL=$(get_service_url "code-pipeline-app" $REVISION_TAG)
fi

echo "🚀 Planner: ${PLANNER_URL}"
echo "🚀 Critic: ${CRITIC_URL}"
echo "🚀 Patcher: ${PATCHER_URL}"
echo "🚀 Code Orchestrator: ${CODE_ORCHESTRATOR_URL}"
echo "🚀 App: ${APP_URL}"