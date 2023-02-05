#!/usr/bin/env python3

import os
import aws_cdk as cdk
from constructs import Construct
from aws_cdk import (
    App, CfnOutput, Stack, Environment, Fn, Duration,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_servicediscovery as servicediscovery,
    aws_iam as iam,
    aws_appmesh as appmesh,
    aws_logs as logs)

# Creating a construct that will populate the required objects created in the platform repo such as vpc, ecs cluster, and service discovery namespace
class BasePlatform(Construct):
    
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)
        environment_name = 'ecsworkshop'

        # The base platform stack is where the VPC was created, so all we need is the name to do a lookup and import it into this stack for use
        self.vpc = ec2.Vpc.from_lookup(
            self, "VPC",
            vpc_name='{}-base/BaseVPC'.format(environment_name)
        )
        
        self.sd_namespace = servicediscovery.PrivateDnsNamespace.from_private_dns_namespace_attributes(
            self, "SDNamespace",
            namespace_name=cdk.Fn.import_value('NSNAME'),
            namespace_arn=cdk.Fn.import_value('NSARN'),
            namespace_id=cdk.Fn.import_value('NSID')
        )
        
        self.ecs_cluster = ecs.Cluster.from_cluster_attributes(
            self, "ECSCluster",
            cluster_name=cdk.Fn.import_value('ECSClusterName'),
            security_groups=[],
            vpc=self.vpc,
            default_cloud_map_namespace=self.sd_namespace
        )
        
        self.services_sec_grp = ec2.SecurityGroup.from_security_group_id(
            self, "ServicesSecGrp",
            security_group_id=cdk.Fn.import_value('ServicesSecGrp')
        )

class FrontendService(Stack):
    
    def __init__(self, scope: Stack, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

#        base_platform = BasePlatform(self, stack_name) 
        self.base_platform = BasePlatform(self, "BasePlatform") 

        self.fargate_task_image = ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
            image=ecs.ContainerImage.from_registry("public.ecr.aws/aws-containers/ecsdemo-frontend"),
            container_port=3000,
            environment={
                "CRYSTAL_URL": "http://ecsdemo-crystal.service.local:3000/crystal",
                "NODEJS_URL": "http://ecsdemo-nodejs.service.local:3000",
                "REGION": os.getenv('AWS_DEFAULT_REGION')
            },
        )

        self.fargate_load_balanced_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, "FrontendFargateLBService",
            service_name='ecsdemo-frontend',
            cluster=self.base_platform.ecs_cluster,
            cpu=256,
            memory_limit_mib=512,
            desired_count=1,
            public_load_balancer=True,
            cloud_map_options=ecs.CloudMapOptions(
                cloud_map_namespace=self.base_platform.sd_namespace
                ),
            task_image_options=self.fargate_task_image
        )
        
        self.fargate_load_balanced_service.task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=['ec2:DescribeSubnets'],
                resources=['*']
            )
        )
        
        self.fargate_load_balanced_service.service.connections.allow_to(
            self.base_platform.services_sec_grp,
            port_range=ec2.Port(protocol=ec2.Protocol.TCP, string_representation="frontendtobackend", from_port=3000, to_port=3000)
        )
        
        # Enable Service Autoscaling
        #self.autoscale = fargate_load_balanced_service.service.auto_scale_task_count(
        #    min_capacity=1,
        #    max_capacity=10
        #)
        
        #self.autoscale.scale_on_cpu_utilization(
        #    "CPUAutoscaling",
        #    target_utilization_percent=50,
        #    scale_in_cooldown=Duration.seconds(30),
        #    scale_out_cooldown=Duration.seconds(30)
        #)

class FrontendServiceMesh(Stack):
    
    def __init__(self, scope: Stack, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.base_platform = BasePlatform(self, stack_name)
        
        self.mesh = appmesh.Mesh.from_mesh_arn(
            self,
            "EcsWorkShop-AppMesh",
            mesh_arn=cdk.Fn.import_value("MeshArn")
        )
        
        self.mesh_vgw = appmesh.VirtualGateway.from_virtual_gateway_attributes(
            self,
            "Mesh-VGW",
            mesh=self.mesh,
            virtual_gateway_name=cdk.Fn.import_value("MeshVGWName")
        )
        
        self.mesh_crystal_vs = appmesh.VirtualService.from_virtual_service_attributes(
            self,
            "mesh-crystal-vs",
            mesh=self.mesh,
            virtual_service_name=cdk.Fn.import_value("MeshCrystalVSName")
        )
        
        self.mesh_nodejs_vs = appmesh.VirtualService.from_virtual_service_attributes(
            self,
            "mesh-nodejs-vs",
            mesh=self.mesh,
            virtual_service_name=cdk.Fn.import_value("MeshNodeJsVSName")
        )
        
        self.fargate_task_def = ecs.TaskDefinition(
            self, "FrontEndTaskDef",
            compatibility=ecs.Compatibility.EC2_AND_FARGATE,
            cpu='256',
            memory_mib='512',
            proxy_configuration=ecs.AppMeshProxyConfiguration( 
                container_name="envoy",
                properties=ecs.AppMeshProxyConfigurationProps(
                    app_ports=[3000],
                    proxy_ingress_port=15000,
                    proxy_egress_port=15001,
                    egress_ignored_i_ps=["169.254.170.2","169.254.169.254"],
                    ignored_uid=1337
                )
            )
        )
        
        self.logGroup = logs.LogGroup(self,"ecsworkshopFrontendLogGroup",
            #log_group_name="ecsworkshop-frontend",
            retention=logs.RetentionDays.ONE_WEEK
        )
        
        self.app_container = self.fargate_task_def.add_container(
            "FrontendServiceContainerDef",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/aws-containers/ecsdemo-frontend"),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix='/frontend-container',
                log_group=self.logGroup
            ),
            essential=True,
            memory_reservation_mib=128,
            environment={
                "CRYSTAL_URL": "http://ecsdemo-crystal.service.local:3000/crystal",
                "NODEJS_URL": "http://ecsdemo-nodejs.service.local:3000",
                "REGION": os.getenv('AWS_DEFAULT_REGION')
            },
            container_name="frontend-app"
        )
        
        self.app_container.add_port_mappings(
            ecs.PortMapping(
                container_port=3000
            )
        )
        
        self.fargate_service = ecs.FargateService(
            self, "FrontEndFargateService",
            service_name='ecsdemo-frontend',
            task_definition=self.fargate_task_def,
            cluster=self.base_platform.ecs_cluster,
            security_group=self.base_platform.services_sec_grp,
            desired_count=1,
            cloud_map_options=ecs.CloudMapOptions(
                cloud_map_namespace=self.base_platform.sd_namespace,
                name='ecsdemo-frontend'
            ),
            #deployment_controller=aws_ecs.DeploymentController(type=aws_ecs.DeploymentControllerType.EXTERNAL)
        )
             
        ##################################################
        ###APP Mesh Configuration####
        
        self.mesh_frontend_vn = appmesh.VirtualNode(
            self,
            "MeshFrontEndNode",
            mesh=self.mesh,
            virtual_node_name="frontend",
            listeners=[appmesh.VirtualNodeListener.http(port=3000)],
            service_discovery=self.appmesh.ServiceDiscovery.cloud_map(self.fargate_service.cloud_map_service),
            backends=[
                appmesh.Backend.virtual_service(self.mesh_crystal_vs),
                appmesh.Backend.virtual_service(self.mesh_nodejs_vs)
                ],
            access_log=appmesh.AccessLog.from_file_path("/dev/stdout")
            
        )
       
        self.envoy_container = self.fargate_task_def.add_container(
            "FrontendServiceProxyContdef",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/appmesh/aws-appmesh-envoy:v1.18.3.0-prod"),
            container_name="envoy",
            memory_reservation_mib=128,
            environment={
                "REGION": os.getenv('AWS_DEFAULT_REGION'),
                "ENVOY_LOG_LEVEL": "critical",
                "ENABLE_ENVOY_STATS_TAGS": "1",
                # "ENABLE_ENVOY_XRAY_TRACING": "1",
                "APPMESH_RESOURCE_ARN": self.mesh_frontend_vn.virtual_node_arn
            },
            essential=True,
            logging=ecs.LogDriver.aws_logs(
                stream_prefix='/mesh-envoy-container',
                log_group=self.logGroup
            ),
            health_check=ecs.HealthCheck(
                interval=cdk.Duration.seconds(5),
                timeout=cdk.Duration.seconds(10),
                retries=10,
                command=["CMD-SHELL","curl -s http://localhost:9901/server_info | grep state | grep -q LIVE"],
            ),
            user="1337"
        )
        
        self.envoy_container.add_ulimits(ecs.Ulimit(
            hard_limit=15000,
            name=ecs.UlimitName.NOFILE,
            soft_limit=15000
            )
        )
        
        self.app_container.add_container_dependencies(ecs.ContainerDependency(
              container=self.envoy_container,
              condition=ecs.ContainerDependencyCondition.HEALTHY
          )
        )
        
        #appmesh-xray-uncomment
        # self.xray_container = self.fargate_task_def.add_container(
        #     "FrontendServiceXrayContdef",
        #     image=ecs.ContainerImage.from_registry("amazon/aws-xray-daemon"),
        #     logging=ecs.LogDriver.aws_logs(
        #         stream_prefix='/xray-container',
        #         log_group=self.logGroup
        #     ),
        #     essential=True,
        #     container_name="xray",
        #     memory_reservation_mib=256,
        #     user="1337"
        # )
        
        # self.envoy_container.add_container_dependencies(ecs.ContainerDependency(
        #       container=xray_container,
        #       condition=ecs.ContainerDependencyCondition.START
        #   )
        # )
        #appmesh-xray-uncomment
        
        self.fargate_task_def.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=['ec2:DescribeSubnets'],
                resources=['*']
            )
        )
        
        self.fargate_service.connections.allow_from_any_ipv4(
            port_range=ec2.Port(protocol=ec2.Protocol.TCP, string_representation="tcp_3000", from_port=3000, to_port=3000),
            description="Allow TCP connections on port 3000"
        )
        
        self.fargate_task_def.execution_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"))
        self.fargate_task_def.execution_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchLogsFullAccess"))
        
        self.fargate_task_def.task_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchFullAccess"))
        # fargate_task_def.task_role.add_managed_policy(aws_iam.ManagedPolicy.from_aws_managed_policy_name("AWSXRayDaemonWriteAccess"))
        self.fargate_task_def.task_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AWSAppMeshEnvoyAccess"))
        
        # Creating a App Mesh virtual router
        meshVR=appmesh.VirtualRouter(
            self,
            "MeshVirtualRouter",
            mesh=self.mesh,
            listeners=[appmesh.VirtualRouterListener.http(3000)],
            virtual_router_name="FrontEnd"
        )
        
        meshVR.add_route(
            "MeshFrontEndVRRoute",
            route_spec=appmesh.RouteSpec.http(
                weighted_targets=[appmesh.WeightedTarget(virtual_node=self.mesh_frontend_vn,weight=1)]
            ),
            route_name="frontend-a"
        )
        
         # Asdding mesh virtual service 
        mesh_frontend_vs = appmesh.VirtualService(self,"mesh-frontend-vs",
            virtual_service_provider=self.appmesh.VirtualServiceProvider.virtual_router(meshVR),
            virtual_service_name="{}.{}".format(self.fargate_service.cloud_map_service.service_name,self.fargate_service.cloud_map_service.namespace.namespace_name)
        )
        
        # Adding Virtual Gateway Route
        self.mesh_gt_router = self.mesh_vgw.add_gateway_route(
            "MeshVGWRouter",
            gateway_route_name="frontend-router",
            route_spec=appmesh.GatewayRouteSpec.http(
                route_target=mesh_frontend_vs
            )
        )
        
        # Enable Service Autoscaling
        self.autoscale = self.fargate_service.auto_scale_task_count(
            min_capacity=3,
            max_capacity=10
        )
        
        self.autoscale.scale_on_cpu_utilization(
            "CPUAutoscaling",
            target_utilization_percent=50,
            scale_in_cooldown=Duration.seconds(30),
            scale_out_cooldown=Duration.seconds(30)
        )
         
        CfnOutput(self, "MeshFrontendVNARN",value=self.mesh_frontend_vn.virtual_node_arn,export_name="MeshFrontendVNARN")
        CfnOutput(self, "MeshFrontendVNName",value=self.mesh_frontend_vn.virtual_node_name,export_name="MeshFrontendVNName")
        CfnOutput(self, "MeshFrontendVGRARN",value=self.mesh_gt_router.gateway_route_arn,export_name="MeshFrontendVGRARN")

_env = Environment(account=os.getenv('AWS_ACCOUNT_ID'), region=os.getenv('AWS_DEFAULT_REGION'))
environment = "ecsworkshop"
stack_name = "{}-frontend".format(environment)
app = App()
FrontendService(app, stack_name, env=_env)

# App Mesh workshop
# FrontendServiceMesh(app, stack_name, env=_env)
app.synth()