#  Copyright 2021 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
Base class for ingesting dashboard services
"""
import traceback
from abc import ABC, abstractmethod
from typing import Any, Iterable, List, Optional, Set

from pydantic import BaseModel

from metadata.generated.schema.api.data.createChart import CreateChartRequest
from metadata.generated.schema.api.data.createDashboard import CreateDashboardRequest
from metadata.generated.schema.api.data.createDashboardDataModel import (
    CreateDashboardDataModelRequest,
)
from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.entity.data.chart import Chart
from metadata.generated.schema.entity.data.dashboard import Dashboard
from metadata.generated.schema.entity.data.dashboardDataModel import DashboardDataModel
from metadata.generated.schema.entity.data.table import Table
from metadata.generated.schema.entity.services.connections.metadata.openMetadataConnection import (
    OpenMetadataConnection,
)
from metadata.generated.schema.entity.services.dashboardService import (
    DashboardConnection,
    DashboardService,
)
from metadata.generated.schema.entity.teams.user import User
from metadata.generated.schema.metadataIngestion.dashboardServiceMetadataPipeline import (
    DashboardServiceMetadataPipeline,
)
from metadata.generated.schema.metadataIngestion.workflow import (
    Source as WorkflowSource,
)
from metadata.generated.schema.type.entityLineage import EntitiesEdge
from metadata.generated.schema.type.entityReference import EntityReference
from metadata.generated.schema.type.usageRequest import UsageRequest
from metadata.ingestion.api.source import Source
from metadata.ingestion.api.topology_runner import TopologyRunnerMixin
from metadata.ingestion.models.delete_entity import (
    DeleteEntity,
    delete_entity_from_source,
)
from metadata.ingestion.models.ometa_classification import OMetaTagAndClassification
from metadata.ingestion.models.topology import (
    NodeStage,
    ServiceTopology,
    TopologyNode,
    create_source_context,
)
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.source.connections import get_connection, get_test_connection_fn
from metadata.utils import fqn
from metadata.utils.filters import filter_by_dashboard
from metadata.utils.logger import ingestion_logger

logger = ingestion_logger()


class DashboardUsage(BaseModel):
    """
    Wrapper to handle type at the sink
    """

    dashboard: Dashboard
    usage: UsageRequest


class DashboardServiceTopology(ServiceTopology):
    """
    Defines the hierarchy in Dashboard Services.
    service -> dashboard -> charts.

    We could have a topology validator. We can only consume
    data that has been produced by any parent node.
    """

    root = TopologyNode(
        producer="get_services",
        stages=[
            NodeStage(
                type_=DashboardService,
                context="dashboard_service",
                processor="yield_create_request_dashboard_service",
                overwrite=False,
                must_return=True,
            ),
            NodeStage(
                type_=OMetaTagAndClassification,
                context="tags",
                processor="yield_tag",
                ack_sink=False,
                nullable=True,
            ),
        ],
        children=["dashboard"],
        post_process=["mark_dashboards_as_deleted"],
    )
    dashboard = TopologyNode(
        producer="get_dashboard",
        stages=[
            NodeStage(
                type_=Chart,
                context="charts",
                processor="yield_dashboard_chart",
                consumer=["dashboard_service"],
                nullable=True,
                cache_all=True,
                clear_cache=True,
            ),
            NodeStage(
                type_=DashboardDataModel,
                context="dataModel",
                processor="yield_datamodel",
                consumer=["dashboard_service"],
            ),
            NodeStage(
                type_=Dashboard,
                context="dashboard",
                processor="yield_dashboard",
                consumer=["dashboard_service"],
            ),
            NodeStage(
                type_=User,
                context="owner",
                processor="process_owner",
                consumer=["dashboard_service"],
            ),
            NodeStage(
                type_=AddLineageRequest,
                context="lineage",
                processor="yield_dashboard_lineage",
                consumer=["dashboard_service"],
                ack_sink=False,
                nullable=True,
            ),
            NodeStage(
                type_=UsageRequest,
                context="usage",
                processor="yield_dashboard_usage",
                consumer=["dashboard_service"],
                ack_sink=False,
                nullable=True,
            ),
        ],
    )


class DashboardServiceSource(TopologyRunnerMixin, Source, ABC):
    """
    Base class for Database Services.
    It implements the topology and context.
    """

    source_config: DashboardServiceMetadataPipeline
    config: WorkflowSource
    metadata: OpenMetadata
    # Big union of types we want to fetch dynamically
    service_connection: DashboardConnection.__fields__["config"].type_

    topology = DashboardServiceTopology()
    context = create_source_context(topology)
    dashboard_source_state: Set = set()

    def __init__(
        self,
        config: WorkflowSource,
        metadata_config: OpenMetadataConnection,
    ):
        super().__init__()
        self.config = config
        self.metadata_config = metadata_config
        self.metadata = OpenMetadata(metadata_config)
        self.service_connection = self.config.serviceConnection.__root__.config
        self.source_config: DashboardServiceMetadataPipeline = (
            self.config.sourceConfig.config
        )
        self.client = get_connection(self.service_connection)

        # Flag the connection for the test connection
        self.connection_obj = self.client
        self.test_connection()

        self.metadata_client = OpenMetadata(self.metadata_config)

    @abstractmethod
    def yield_dashboard(
        self, dashboard_details: Any
    ) -> Iterable[CreateDashboardRequest]:
        """
        Method to Get Dashboard Entity
        """

    @abstractmethod
    def yield_dashboard_lineage_details(
        self, dashboard_details: Any, db_service_name: str
    ) -> Optional[Iterable[AddLineageRequest]]:
        """
        Get lineage between dashboard and data sources
        """

    @abstractmethod
    def yield_dashboard_chart(
        self, dashboard_details: Any
    ) -> Optional[Iterable[CreateChartRequest]]:
        """
        Method to fetch charts linked to dashboard
        """

    @abstractmethod
    def get_dashboards_list(self) -> Optional[List[Any]]:
        """
        Get List of all dashboards
        """

    @abstractmethod
    def get_dashboard_name(self, dashboard: Any) -> str:
        """
        Get Dashboard Name from each element coming from `get_dashboards_list`
        """

    @abstractmethod
    def get_dashboard_details(self, dashboard: Any) -> Any:
        """
        Get Dashboard Details
        """

    def yield_datamodel(self, _) -> Optional[Iterable[CreateDashboardDataModelRequest]]:
        """
        Method to fetch DataModel linked to Dashboard
        """

        logger.debug(
            f"DataModel is not supported for {self.service_connection.type.name}"
        )

    def yield_dashboard_lineage(
        self, dashboard_details: Any
    ) -> Optional[Iterable[AddLineageRequest]]:
        """
        Yields lineage if config is enabled.

        We will look for the data in all the services
        we have informed.
        """
        for db_service_name in self.source_config.dbServiceNames or []:
            yield from self.yield_dashboard_lineage_details(
                dashboard_details, db_service_name
            ) or []

    def yield_tag(
        self, *args, **kwargs  # pylint: disable=W0613
    ) -> Optional[Iterable[OMetaTagAndClassification]]:
        """
        Method to fetch dashboard tags
        """
        return  # Dashboard does not support fetching tags except Tableau and Redash

    def yield_dashboard_usage(
        self, *args, **kwargs  # pylint: disable=W0613
    ) -> Optional[Iterable[DashboardUsage]]:
        """
        Method to pick up dashboard usage data
        """
        return  # Dashboard usage currently only available for Looker

    def close(self):
        self.metadata.close()

    def get_services(self) -> Iterable[WorkflowSource]:
        yield self.config

    def yield_create_request_dashboard_service(self, config: WorkflowSource):
        yield self.metadata.get_create_service_from_source(
            entity=DashboardService, config=config
        )

    def mark_dashboards_as_deleted(self) -> Iterable[DeleteEntity]:
        """
        Method to mark the dashboards as deleted
        """
        if self.source_config.markDeletedDashboards:
            logger.info("Mark Deleted Dashboards set to True")
            yield from delete_entity_from_source(
                metadata=self.metadata,
                entity_type=Dashboard,
                entity_source_state=self.dashboard_source_state,
                mark_deleted_entity=self.source_config.markDeletedDashboards,
                params={
                    "service": self.context.dashboard_service.fullyQualifiedName.__root__
                },
            )

    def process_owner(self, dashboard_details):
        try:
            owner = self.get_owner_details(  # pylint: disable=assignment-from-none
                dashboard_details=dashboard_details
            )
            if owner and self.source_config.overrideOwner:
                self.metadata.patch_owner(
                    entity=Dashboard,
                    entity_id=self.context.dashboard.id,
                    owner=owner,
                    force=True,
                )
        except Exception as exc:
            logger.debug(traceback.format_exc())
            logger.warning(f"Error processing owner for {dashboard_details}: {exc}")

    def register_record(self, dashboard_request: CreateDashboardRequest) -> None:
        """
        Mark the dashboard record as scanned and update the dashboard_source_state
        """
        dashboard_fqn = fqn.build(
            self.metadata,
            entity_type=Dashboard,
            service_name=dashboard_request.service.__root__,
            dashboard_name=dashboard_request.name.__root__,
        )

        self.dashboard_source_state.add(dashboard_fqn)
        self.status.scanned(dashboard_fqn)

    def get_owner_details(  # pylint: disable=useless-return
        self, dashboard_details  # pylint: disable=unused-argument
    ) -> Optional[EntityReference]:
        """Get dashboard owner

        Args:
            dashboard_details:
        Returns:
            Optional[EntityReference]
        """
        logger.debug(
            f"Processing ownership is not supported for {self.service_connection.type.name}"
        )
        return None

    @staticmethod
    def _get_add_lineage_request(
        to_entity: Dashboard, from_entity: Table
    ) -> Optional[AddLineageRequest]:
        if from_entity and to_entity:
            return AddLineageRequest(
                edge=EntitiesEdge(
                    fromEntity=EntityReference(
                        id=from_entity.id.__root__, type="table"
                    ),
                    toEntity=EntityReference(
                        id=to_entity.id.__root__, type="dashboard"
                    ),
                )
            )
        return None

    def get_dashboard(self) -> Any:
        """
        Method to iterate through dashboard lists filter dashboards & yield dashboard details
        """
        for dashboard in self.get_dashboards_list():

            dashboard_name = self.get_dashboard_name(dashboard)
            if filter_by_dashboard(
                self.source_config.dashboardFilterPattern,
                dashboard_name,
            ):
                self.status.filter(
                    dashboard_name,
                    "Dashboard Filtered Out",
                )
                continue

            try:
                dashboard_details = self.get_dashboard_details(dashboard)
            except Exception as exc:
                logger.debug(traceback.format_exc())
                logger.warning(
                    f"Cannot extract dashboard details from {dashboard}: {exc}"
                )
                continue

            yield dashboard_details

    def test_connection(self) -> None:
        test_connection_fn = get_test_connection_fn(self.service_connection)
        test_connection_fn(self.metadata, self.connection_obj, self.service_connection)

    def prepare(self):
        pass
