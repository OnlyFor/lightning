# Copyright The Lightning AI team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from datetime import timedelta
from re import escape
from unittest import mock
from unittest.mock import Mock

import pytest
import torch
import torch.nn as nn

from lightning import LightningModule
from lightning.pytorch.plugins.environments import LightningEnvironment
from lightning.pytorch.strategies import ModelParallelStrategy
from torch.optim import Adam

from tests_fabric.helpers.runif import RunIf


@mock.patch("lightning.pytorch.strategies.model_parallel._TORCH_GREATER_EQUAL_2_3", False)
def test_torch_greater_equal_2_3():
    with pytest.raises(ImportError, match="ModelParallelStrategy requires PyTorch 2.3 or higher"):
        ModelParallelStrategy()


@RunIf(min_torch="2.3")
def test_device_mesh_access():
    strategy = ModelParallelStrategy()
    with pytest.raises(RuntimeError, match="Accessing the device mesh .* not allowed"):
        _ = strategy.device_mesh


@RunIf(min_torch="2.3")
@pytest.mark.parametrize(
    ("num_nodes", "devices", "invalid_dp_size", "invalid_tp_size"),
    [
        (1, 4, 1, 1),
        (1, 4, 2, 3),
        (1, 4, 4, 2),
        (2, 4, 1, 4),
        (2, 4, 2, 1),
    ],
)
def test_validate_device_mesh_dimensions(num_nodes, devices, invalid_dp_size, invalid_tp_size):
    """Test passing sizes that don't multiply to the world size raises an error."""
    strategy = ModelParallelStrategy(
        data_parallel_size=invalid_dp_size,
        tensor_parallel_size=invalid_tp_size,
    )
    strategy._setup_distributed = Mock()
    strategy._accelerator = Mock()
    strategy.cluster_environment = Mock(
        world_size=Mock(return_value=(num_nodes * devices)), local_rank=Mock(return_value=1)
    )
    strategy.parallel_devices = [torch.device("cpu")] * devices
    strategy.num_nodes = num_nodes
    with pytest.raises(RuntimeError, match="multiplied should equal the world size"):
        strategy.setup_environment()


@RunIf(min_torch="2.3")
def test_checkpoint_io_unsupported():
    """Test that the ModelParallel strategy does not support the `CheckpointIO` plugin."""
    strategy = ModelParallelStrategy()
    with pytest.raises(NotImplementedError, match="does not use the `CheckpointIO` plugin"):
        _ = strategy.checkpoint_io

    with pytest.raises(NotImplementedError, match="does not support setting a `CheckpointIO` plugin"):
        strategy.checkpoint_io = Mock()


@RunIf(min_torch="2.3")
def test_fsdp_v1_modules_unsupported():
    """Test that the strategy won't allow setting up a module wrapped with the legacy FSDP API."""
    from torch.distributed.fsdp import FullyShardedDataParallel

    class Model(LightningModule):
        def configure_model(self):
            pass

    model = Model()
    model.modules = Mock(return_value=[Mock(spec=FullyShardedDataParallel)])
    strategy = ModelParallelStrategy()
    strategy.model = model
    strategy._lightning_module = model
    strategy._accelerator = Mock()

    with pytest.raises(TypeError, match="only supports the new FSDP2 APIs in PyTorch >= 2.3"):
        strategy.setup(Mock())


@RunIf(min_torch="2.3")
def test_configure_model_required():
    class Model1(LightningModule):
        pass

    class Model2(LightningModule):
        def configure_model(self):
            pass

    model = Model1()
    strategy = ModelParallelStrategy()
    strategy.model = model
    strategy._lightning_module = model
    strategy._accelerator = Mock()
    strategy._parallel_devices = [torch.device("cpu")]

    with pytest.raises(TypeError, match="you are required to override the `configure_model"):
        strategy.setup(Mock())

    model = Model2()
    strategy.model = model
    strategy._lightning_module = model
    strategy.setup(Mock())


@RunIf(min_torch="2.3")
def test_save_checkpoint_storage_options(tmp_path):
    """Test that the strategy does not accept storage options for saving checkpoints."""
    strategy = ModelParallelStrategy()
    with pytest.raises(
        TypeError, match=escape("ModelParallelStrategy.save_checkpoint(..., storage_options=...)` is not")
    ):
        strategy.save_checkpoint(checkpoint=Mock(), filepath=tmp_path, storage_options=Mock())


@RunIf(min_torch="2.3")
def test_save_checkpoint_path_exists():
    pytest.skip("Checkpoint saving and loading not implemented")


@RunIf(min_torch="2.3")
def test_load_full_checkpoint_support():
    pytest.skip("Checkpoint saving and loading not implemented")


@RunIf(min_torch="2.3")
def test_load_unknown_checkpoint_type():
    pytest.skip("Checkpoint saving and loading not implemented")


@RunIf(min_torch="2.3")
@mock.patch("lightning.pytorch.strategies.model_parallel._setup_device_mesh")
@mock.patch("torch.distributed.init_process_group")
def test_set_timeout(init_process_group_mock, _):
    """Test that the timeout gets passed to the ``torch.distributed.init_process_group`` function."""
    test_timedelta = timedelta(seconds=30)
    strategy = ModelParallelStrategy(timeout=test_timedelta)
    strategy._lightning_module = Mock()
    strategy.parallel_devices = [torch.device("cpu")]
    strategy.cluster_environment = LightningEnvironment()
    strategy.accelerator = Mock()
    strategy.setup_environment()
    process_group_backend = strategy._get_process_group_backend()
    global_rank = strategy.cluster_environment.global_rank()
    world_size = strategy.cluster_environment.world_size()
    init_process_group_mock.assert_called_with(
        process_group_backend, rank=global_rank, world_size=world_size, timeout=test_timedelta
    )


@RunIf(min_torch="2.3")
def test_meta_device_materialization():
    """Test that the `setup()` method materializes meta-device tensors in the LightningModule."""

    class NoResetParameters(nn.Module):
        def __init__(self):
            super().__init__()
            self.weight = nn.Parameter(torch.ones(4, 4))

    class CustomModel(LightningModule):
        def __init__(self):
            super().__init__()
            # nn.Sequential as a parameterless module
            self.layer1 = nn.Sequential(NoResetParameters(), NoResetParameters())
            self.layer2 = nn.Linear(4, 4)
            self.register_buffer("buffer", torch.rand(2))

        def reset_parameters(self):
            self.buffer.fill_(1.0)

        def configure_model(self) -> None:
            pass

    with torch.device("meta"):
        model = CustomModel()
    assert model.layer1[0].weight.is_meta
    assert model.layer2.weight.is_meta
    assert model.buffer.is_meta

    strategy = ModelParallelStrategy()
    strategy._accelerator = Mock()
    strategy._device_mesh = Mock()
    strategy._parallel_devices = [torch.device("cpu")]
    strategy._lightning_module = model
    strategy.model = model

    with pytest.warns(UserWarning, match=r"`reset_parameters\(\)` method for re-initialization: NoResetParameters"):
        strategy.setup(Mock())
    assert all(not p.is_meta for p in model.parameters())
    assert all(not b.is_meta for b in model.buffers())
