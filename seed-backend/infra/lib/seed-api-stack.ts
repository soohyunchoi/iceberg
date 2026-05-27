import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';
import { PineconeIndex } from './pinecone-custom-resource';

interface SeedApiStackProps extends cdk.StackProps {
  stageName: string;
  similarityThresholdAuto: string;
  similarityThresholdMin: string;
  modelS3Key: string;
}

export class SeedApiStack extends cdk.Stack {
  public readonly api: apigateway.RestApi;
  public readonly lambdaFn: lambda.Function;
  public readonly table: dynamodb.Table;

  constructor(scope: Construct, id: string, props: SeedApiStackProps) {
    super(scope, id, props);

    // ── DynamoDB ──
    this.table = new dynamodb.Table(this, 'SeedPrimary', {
      tableName: `seed-primary-${props.stageName}`,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy:
        props.stageName === 'prod'
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
    });

    this.table.addGlobalSecondaryIndex({
      indexName: 'GSI1',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── S3: Model Weights ──
    const modelBucket = new s3.Bucket(this, 'ModelWeights', {
      bucketName: `seed-models-${props.stageName}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    });

    // ── Secrets Manager (pre-created out of band; see README) ──
    const pineconeSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'PineconeKey',
      `seed/pinecone-api-key-${props.stageName}`
    );
    const anthropicSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'AnthropicKey',
      `seed/anthropic-api-key-${props.stageName}`
    );

    // ── Cognito ──
    const userPool = new cognito.UserPool(this, 'SeedUsers', {
      userPoolName: `seed-users-${props.stageName}`,
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy:
        props.stageName === 'prod'
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
    });

    const userPoolClient = userPool.addClient('SeedClient', {
      // userPassword enables the server-side /auth/login wrapper (USER_PASSWORD_AUTH).
      authFlows: { userSrp: true, userPassword: true },
      accessTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
    });

    // ── ECR Repository ──
    const ecrRepo = new ecr.Repository(this, 'LambdaRepo', {
      repositoryName: `seed-lambda-${props.stageName}`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
    });

    // ── Lambda ──
    this.lambdaFn = new lambda.DockerImageFunction(this, 'SeedApi', {
      functionName: `seed-api-${props.stageName}`,
      code: lambda.DockerImageCode.fromEcr(ecrRepo, {
        tagOrDigest: process.env.IMAGE_TAG || 'latest',
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: 1024,
      timeout: cdk.Duration.seconds(30),
      environment: {
        DDB_TABLE_NAME: this.table.tableName,
        COGNITO_USER_POOL_ID: userPool.userPoolId,
        COGNITO_CLIENT_ID: userPoolClient.userPoolClientId,
        MODEL_S3_BUCKET: modelBucket.bucketName,
        MODEL_S3_KEY: props.modelS3Key,
        PINECONE_API_KEY_SECRET: pineconeSecret.secretArn,
        ANTHROPIC_API_KEY_SECRET: anthropicSecret.secretArn,
        PINECONE_INDEX_NAME: `seed-canonicals-${props.stageName}`,
        SIMILARITY_THRESHOLD_AUTO: props.similarityThresholdAuto,
        SIMILARITY_THRESHOLD_MIN: props.similarityThresholdMin,
        POWERTOOLS_SERVICE_NAME: 'seed-api',
      },
    });

    // IAM — CDK auto-generates least-privilege policies
    this.table.grantReadWriteData(this.lambdaFn);
    modelBucket.grantRead(this.lambdaFn);
    pineconeSecret.grantRead(this.lambdaFn);
    anthropicSecret.grantRead(this.lambdaFn);

    // Cognito admin actions used by the /auth/* wrapper
    this.lambdaFn.addToRolePolicy(
      new cdk.aws_iam.PolicyStatement({
        actions: [
          'cognito-idp:SignUp',
          'cognito-idp:InitiateAuth',
          'cognito-idp:ConfirmSignUp',
          'cognito-idp:ResendConfirmationCode',
        ],
        resources: [userPool.userPoolArn],
      })
    );

    // ── Pinecone Serverless index (custom resource) ──
    new PineconeIndex(this, 'SeedCanonicalsIndex', {
      indexName: `seed-canonicals-${props.stageName}`,
      dimension: 384,
      metric: 'cosine',
      cloud: 'aws',
      region: 'us-east-1',
      apiKeySecretArn: pineconeSecret.secretArn,
    });

    // ── API Gateway ──
    this.api = new apigateway.RestApi(this, 'SeedRestApi', {
      restApiName: `seed-api-${props.stageName}`,
      description: 'SEED Thought Journaling API',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: {
        stageName: props.stageName,
        throttlingRateLimit: 10,
        throttlingBurstLimit: 20,
      },
    });

    const cognitoAuthorizer = new apigateway.CognitoUserPoolsAuthorizer(
      this,
      'CognitoAuth',
      { cognitoUserPools: [userPool] }
    );

    const lambdaIntegration = new apigateway.LambdaIntegration(this.lambdaFn);

    // Auth routes (no authorizer)
    const auth = this.api.root.addResource('auth');
    auth.addResource('signup').addMethod('POST', lambdaIntegration);
    auth.addResource('login').addMethod('POST', lambdaIntegration);
    auth.addResource('refresh').addMethod('POST', lambdaIntegration);

    // Protected routes
    const authOpts = {
      authorizer: cognitoAuthorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    };

    const thoughts = this.api.root.addResource('thoughts');
    thoughts.addMethod('POST', lambdaIntegration, authOpts);

    const thoughtsMine = thoughts.addResource('mine');
    thoughtsMine.addMethod('GET', lambdaIntegration, authOpts);

    const thoughtsToday = thoughts.addResource('today');
    thoughtsToday.addMethod('GET', lambdaIntegration, authOpts);

    const thoughtsConfirm = thoughts.addResource('confirm');
    thoughtsConfirm.addMethod('POST', lambdaIntegration, authOpts);

    const rooms = this.api.root.addResource('rooms');
    const roomById = rooms.addResource('{canonical_id}');
    roomById.addMethod('GET', lambdaIntegration, authOpts);

    const roomThoughts = roomById.addResource('thoughts');
    roomThoughts.addMethod('GET', lambdaIntegration, authOpts);

    // Usage Plan
    const usagePlan = this.api.addUsagePlan('StandardPlan', {
      name: `seed-standard-${props.stageName}`,
      throttle: { rateLimit: 10, burstLimit: 20 },
      quota: { limit: 5000, period: apigateway.Period.DAY },
    });
    usagePlan.addApiStage({ stage: this.api.deploymentStage });

    // ── Outputs ──
    new cdk.CfnOutput(this, 'ApiUrl', { value: this.api.url });
    new cdk.CfnOutput(this, 'UserPoolId', { value: userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: userPoolClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, 'TableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'EcrRepoUri', { value: ecrRepo.repositoryUri });
  }
}
