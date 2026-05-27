import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

interface PineconeIndexProps {
  indexName: string;
  dimension: number;
  metric: string;
  cloud: string;
  region: string;
  apiKeySecretArn: string;
}

/**
 * Manages the lifecycle of a Pinecone Serverless index via a CDK custom
 * resource. Pinecone has no native CDK construct, so a small Lambda calls the
 * Pinecone REST API on create/update/delete during cdk deploy / destroy.
 * Handler source lives in infra/lambda/pinecone-cr/index.py.
 */
export class PineconeIndex extends Construct {
  constructor(scope: Construct, id: string, props: PineconeIndexProps) {
    super(scope, id);

    const onEvent = new cdk.aws_lambda.Function(this, 'PineconeHandler', {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.seconds(30),
      code: cdk.aws_lambda.Code.fromAsset(
        path.join(__dirname, '..', 'lambda', 'pinecone-cr')
      ),
      environment: {
        API_KEY_SECRET_ARN: props.apiKeySecretArn,
      },
    });

    onEvent.addToRolePolicy(
      new cdk.aws_iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [props.apiKeySecretArn],
      })
    );

    const provider = new cr.Provider(this, 'PineconeProvider', {
      onEventHandler: onEvent,
    });

    new cdk.CustomResource(this, 'PineconeIndexResource', {
      serviceToken: provider.serviceToken,
      properties: {
        IndexName: props.indexName,
        Dimension: props.dimension.toString(),
        Metric: props.metric,
        Cloud: props.cloud,
        Region: props.region,
      },
    });
  }
}
