
"""
Regridding module file for regridding input forcing files.
"""
import os
import sys
import traceback
import time
from datetime import datetime, timedelta
#from mpi4py.futures import MPIPoolExecutor
from mpi4py.futures import MPICommExecutor
#import mpi4py.util.pool as mpi_pool

try:
    import esmpy as ESMF
except ImportError:
    import ESMF

import numpy as np
import netCDF4 as nc
import pandas as pd

from . import err_handler
from . import ioMod
from . import timeInterpMod

import dask
import dask.delayed
import time

if "WGRIB2" not in os.environ:
    WGRIB2_env = False
else:
    WGRIB2_env = True

NETCDF = "NETCDF"
GRIB2 = "GRIB2"

next_file_number = 0


def mkfilename():
    global next_file_number
    next_file_number += 1
    return '{}'.format(next_file_number)


def static_vars(**kwargs):
    def decorate(func):
        for k in kwargs:
            setattr(func, k, kwargs[k])
        return func

    return decorate


def create_link(name, input_file, tmpFile, config_options, mpi_config):
    if mpi_config.rank == 0:
        try:
            config_options.statusMsg = name + " file being used: " + input_file
            err_handler.log_msg(config_options, mpi_config)

            os.symlink(input_file, tmpFile)
        except:
            config_options.errMsg = "Unable to create link: " + input_file + " to: " + tmpFile
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

@dask.delayed
def compute(id_tmp,nc_var):
    return id_tmp[nc_var].to_masked_array()

def regrid_ak_ext_ana(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of Alaska ExtAna data. Data was already regridded in the prior run of the AnA stage so just read data in.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in2):
        if mpi_config.rank == 0:
            config_options.statusMsg = "No AK AnA in_2 file found for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        err_handler.log_msg(config_options, mpi_config)
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No AK AnA regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        err_handler.log_msg(config_options, mpi_config)
        return

    if mpi_config.rank == 0:
        from netCDF4 import Dataset
        try:
            ds = Dataset(input_forcings.file_in2,'r')
        except:
            config_options.errMsg = f"Unable to open input NetCDF file: {input_forcings.file_in}"
            err_handler.log_critical(config_options, mpi_config)
        
        ds.set_auto_scale(True)
        ds.set_auto_mask(False)

    if input_forcings.nx_global is None or input_forcings.ny_global is None:
        # This is the first timestep.
        if mpi_config.rank == 0:
            input_forcings.ny_global = ds.dimensions['y'].size
            input_forcings.nx_global = ds.dimensions['x'].size

        input_forcings.ny_global = mpi_config.broadcast_parameter(input_forcings.ny_global,
                                                                  config_options, param_type=int)
        err_handler.check_program_status(config_options, mpi_config)
        input_forcings.nx_global = mpi_config.broadcast_parameter(input_forcings.nx_global,
                                                                  config_options, param_type=int)
        err_handler.check_program_status(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            try:
            # noinspection PyTypeChecker
                input_forcings.esmf_grid_in = ESMF.Grid(np.array([input_forcings.ny_global, input_forcings.nx_global]),
                                                        staggerloc=ESMF.StaggerLoc.CENTER,
                                                        coord_sys=ESMF.CoordSys.SPH_DEG)
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = f"Unable to create source ESMF grid from netCDF file: {input_forcings.file_in} ({str(esmf_error)})"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.x_lower_bound = input_forcings.esmf_grid_in.lower_bounds[ESMF.StaggerLoc.CENTER][1]
                input_forcings.x_upper_bound = input_forcings.esmf_grid_in.upper_bounds[ESMF.StaggerLoc.CENTER][1]
                input_forcings.y_lower_bound = input_forcings.esmf_grid_in.lower_bounds[ESMF.StaggerLoc.CENTER][0]
                input_forcings.y_upper_bound = input_forcings.esmf_grid_in.upper_bounds[ESMF.StaggerLoc.CENTER][0]
                input_forcings.nx_local = input_forcings.x_upper_bound - input_forcings.x_lower_bound
                input_forcings.ny_local = input_forcings.y_upper_bound - input_forcings.y_lower_bound
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = f"Unable to extract local X/Y boundaries from global grid from netCDF file: {input_forcings.file_in} ({str(err)})"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
        elif(config_options.grid_type == "unstructured"):
            try:
                input_forcings.esmf_grid_in = ESMF.Mesh(filename=config_options.geogrid,filetype=ESMF.FileFormat.ESMFMESH)
                input_forcings.esmf_grid_in_elem = ESMF.Mesh(filename=config_options.geogrid,filetype=ESMF.FileFormat.ESMFMESH)
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = f"Unable to create source ESMF Mesh from netCDF file: {input_forcings.file_in} ({str(esmf_error)})"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.nx_local = len(input_forcings.esmf_grid_in.esmf_grid_in.coords[0][1])
                input_forcings.ny_local = len(input_forcings.esmf_grid_in.esmf_grid_in.coords[0][1])
                input_forcings.nx_local_elem = len(input_forcings.esmf_grid_in_elem.esmf_grid_in.coords[0][1])
                input_forcings.ny_local_elem = len(input_forcings.esmf_grid_in_elem.esmf_grid_in.coords[0][1])
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = f"Unable to extract local X/Y boundaries from global mesh file: {input_forcings.file_in} ({str(err)})"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
        elif(config_options.grid_type == "hydrofabric"):
            try:
                input_forcings.esmf_grid_in = ESMF.Mesh(filename=config_options.geogrid,filetype=ESMF.FileFormat.ESMFMESH)
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = f"Unable to create source ESMF Mesh from netCDF file: {input_forcings.file_in} ({str(esmf_error)})"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.nx_local = len(input_forcings.esmf_grid_in.esmf_grid_in.coords[0][1])
                input_forcings.ny_local = len(input_forcings.esmf_grid_in.esmf_grid_in.coords[0][1])
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = f"Unable to extract local X/Y boundaries from global mesh file: {input_forcings.file_in} ({str(err)})"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)



        # Create out regridded numpy arrays to hold the regridded data.
        if(config_options.grid_type == "gridded"):
            input_forcings.regridded_forcings1 = np.empty([9, wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                          np.float32)
            input_forcings.regridded_forcings2 = np.empty([9, wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                          np.float32)
        elif(config_options.grid_type == "unstructured"):
            input_forcings.regridded_forcings1 = np.empty([9, wrf_hydro_geo_meta.ny_local],np.float32)
            input_forcings.regridded_forcings2 = np.empty([9, wrf_hydro_geo_meta.ny_local],np.float32)
            input_forcings.regridded_forcings1_elem = np.empty([9, wrf_hydro_geo_meta.ny_local_elem],np.float32)
            input_forcings.regridded_forcings2_elem = np.empty([9, wrf_hydro_geo_meta.ny_local_elem],np.float32)
        elif(config_options.grid_type == "unstructured"):
            input_forcings.regridded_forcings1 = np.empty([9, wrf_hydro_geo_meta.ny_local],np.float32)
            input_forcings.regridded_forcings2 = np.empty([9, wrf_hydro_geo_meta.ny_local],np.float32)


    for force_count, nc_var in enumerate(input_forcings.netcdf_var_names):
        var_tmp = None
        var_tmp_elem = None
        if mpi_config.rank == 0:
            config_options.statusMsg = f"Processing input AK AnA variable: {nc_var} from {input_forcings.file_in2}"
            err_handler.log_msg(config_options, mpi_config)
            print(config_options.statusMsg,flush=True)
            if(config_options.grid_type == "gridded"):
                try:
                    var_tmp = ds.variables[nc_var][0, :, :]
                    var_tmp = np.float32(var_tmp)

                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = f"Unable to extract: {nc_var} from: {input_forcings.file_in2} ({str(err)})"
                    err_handler.log_critical(config_options, mpi_config)
            elif(config_options.grid_type == "unstructured"):
                try:
                    var_tmp = ds.variables[nc_var][0, :]
                    var_tmp = np.float32(var_tmp)

                    var_tmp_elem = ds.variables[nc_var][0, :]
                    var_tmp_elem = np.float32(var_tmp_elem)     
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = f"Unable to extract: {nc_var} from: {input_forcings.file_in2} ({str(err)})"
                    err_handler.log_critical(config_options, mpi_config)
            elif(config_options.grid_type == "hydrofabric"):
                try:
                    var_tmp = ds.variables[nc_var][0, :]
                    var_tmp = np.float32(var_tmp)

                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = f"Unable to extract: {nc_var} from: {input_forcings.file_in2} ({str(err)})"
                    err_handler.log_critical(config_options, mpi_config)
            
        err_handler.check_program_status(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)
            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract ExtAnA forcing data from the AK AnA field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
        elif(config_options.grid_type == "unstructured"):
            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = var_tmp[wrf_hydro_geo_meta.mesh_inds]
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :, :] = var_tmp[wrf_hydro_geo_meta.mesh_inds_elem]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract ExtAnA forcing data from the AK AnA mesh field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
        elif(config_options.grid_type == "hydrofabric"):
            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = var_tmp[wrf_hydro_geo_meta.mesh_inds]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract ExtAnA forcing data from the AK AnA mesh field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]

    if mpi_config.rank == 0:
        ds.close()


def _regrid_ak_ext_ana_pcp_stage4(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of Alaska ExtAna supplemental Stage IV precip data.
    :param supplemental_precip:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """

    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.exists(supplemental_precip.file_in1):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if supplemental_precip.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No StageIV regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return

    # Create a path for a temporary NetCDF files that will
    # be created through the wgrib2 process.
    stage4_tmp_nc = config_options.scratch_dir + "/STAGEIV_TMP-{}.nc".format(mkfilename())

    lat_var = "latitude"
    lon_var = "longitude"
    
    if supplemental_precip.fileType != NETCDF:
        # This file shouldn't exist.... but if it does (previously failed
        # execution of the program), remove it.....
        if mpi_config.rank == 0:
            if os.path.isfile(stage4_tmp_nc):
                config_options.statusMsg = "Found old temporary file: " + stage4_tmp_nc + " - Removing....."
                err_handler.log_warning(config_options, mpi_config)
                try:
                    os.remove(stage4_tmp_nc)
                except OSError:
                    config_options.errMsg = f"Unable to remove temporary file: {stage4_tmp_nc}"
                    err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Create a temporary NetCDF file from the GRIB2 file.
        if(WGRIB2_env):
            cmd = f'$WGRIB2 -match "APCP:surface:0-6 hour acc fcst" {supplemental_precip.file_in2} -netcdf {stage4_tmp_nc}'
        else:
            cmd = "APCP:surface:0-6 hour acc fcst"

        if mpi_config.rank == 0:
            config_options.statusMsg = f"WGRIB2 command: {cmd}"
            err_handler.log_msg(config_options, mpi_config)
        id_tmp = ioMod.open_grib2(supplemental_precip.file_in2, stage4_tmp_nc, cmd,
                                  config_options, mpi_config, inputVar=None, special_case=False)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        create_link("STAGEIV-PCP", supplemental_precip.file_in2, stage4_tmp_nc, config_options, mpi_config)
        id_tmp = ioMod.open_netcdf_forcing(stage4_tmp_nc, config_options, mpi_config, False, lat_var, lon_var)

    # Check to see if we need to calculate regridding weights.
    calc_regrid_flag = check_supp_pcp_regrid_status(id_tmp, supplemental_precip, config_options,
                                                    wrf_hydro_geo_meta, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    if calc_regrid_flag:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Calculating STAGE IV regridding weights."
            err_handler.log_msg(config_options, mpi_config)
        calculate_supp_pcp_weights(supplemental_precip, id_tmp, stage4_tmp_nc, config_options, mpi_config, lat_var, lon_var)
        err_handler.check_program_status(config_options, mpi_config)

    if(config_options.grid_type == "gridded"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding STAGE IV '{supplemental_precip.netcdf_var_names[-1]}' Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables[supplemental_precip.netcdf_var_names[-1]][0,:,:]
                var_tmp = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,var_tmp)
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from STAGE IV file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)
    elif(config_options.grid_type == "unstructured"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding STAGE IV '{supplemental_precip.netcdf_var_names[-1]}' Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables[supplemental_precip.netcdf_var_names[-1]][0,:,:]
                var_tmp = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,var_tmp)
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from STAGE IV file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        # Regrid the input variables.
        var_tmp_elem = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding STAGE IV '{supplemental_precip.netcdf_var_names[-1]}' Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp_elem = id_tmp.variables[supplemental_precip.netcdf_var_names[-1]][0,:,:]
                var_tmp = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,var_tmp)
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from STAGE IV file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp_elem = mpi_config.scatter_array(supplemental_precip, var_tmp_elem, config_options)
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "hydrofabric"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding STAGE IV '{supplemental_precip.netcdf_var_names[-1]}' Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables[supplemental_precip.netcdf_var_names[-1]][0,:,:]
                var_tmp = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,var_tmp)
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from STAGE IV file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

    if(config_options.grid_type == "gridded"):
        try:
            supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place STAGE IV precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid STAGE IV supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:, :] = supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the 6-hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
            supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:, :] = \
                supplemental_precip.regridded_precip2[:, :]
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "unstructured"):
        try:
            supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place STAGE IV precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid STAGE IV supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:] = supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the 6-hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
            supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:] = \
                supplemental_precip.regridded_precip2[:]
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place STAGE IV precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out_elem = supplemental_precip.regridObj_elem(supplemental_precip.esmf_field_in_elem,
                                                                               supplemental_precip.esmf_field_out_elem)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid STAGE IV supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out_elem.data[np.where(supplemental_precip.regridded_mask_elem == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2_elem[:] = supplemental_precip.esmf_field_out_elem.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the 6-hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2_elem != config_options.globalNdv)
            supplemental_precip.regridded_precip2_elem[ind_valid] = supplemental_precip.regridded_precip2_elem[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1_elem[:] = \
                supplemental_precip.regridded_precip2_elem[:]
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "hydrofabric"):
        try:
            supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place STAGE IV precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid STAGE IV supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:] = supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the 6-hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
            supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:] = \
                supplemental_precip.regridded_precip2[:]
        err_handler.check_program_status(config_options, mpi_config)

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + stage4_tmp_nc
            err_handler.log_critical(config_options, mpi_config)
        try:
            os.remove(stage4_tmp_nc)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + stage4_tmp_nc
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)


def regrid_ak_ext_ana_pcp(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of Alaska ExtAna supplemental precip data.
    :param supplemental_precip:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """

    if supplemental_precip.ext_ana == "STAGE4":
        supplemental_precip.netcdf_var_names.append('APCP_surface')
        #supplemental_precip.netcdf_var_names.append('A_PCP_GDS5_SFC_acc6h')
        _regrid_ak_ext_ana_pcp_stage4(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config)
        supplemental_precip.netcdf_var_names.pop()
    else: #MRMS
        supplemental_precip.netcdf_var_names.append('MultiSensorQPE01H_0mabovemeansealevel')
        #supplemental_precip.netcdf_var_names.append('A_PCP_GDS5_SFC_acc6h')
        regrid_mrms_hourly(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config)
        supplemental_precip.netcdf_var_names.pop()


def _regrid_conus_ext_ana_pcp_stage4(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of Alaska ExtAna supplemental Stage IV precip data.
    :param supplemental_precip:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """

    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.exists(supplemental_precip.file_in1):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if supplemental_precip.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No StageIV regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return

    # Create a path for a temporary NetCDF files that will
    # be created through the wgrib2 process.
    stage4_tmp_nc = config_options.scratch_dir + "/STAGEIV_TMP-{}.nc".format(mkfilename())

    lat_var = "latitude"
    lon_var = "longitude"

    if supplemental_precip.fileType != NETCDF:
        # This file shouldn't exist.... but if it does (previously failed
        # execution of the program), remove it.....
        if mpi_config.rank == 0:
            if os.path.isfile(stage4_tmp_nc):
                config_options.statusMsg = "Found old temporary file: " + stage4_tmp_nc + " - Removing....."
                err_handler.log_warning(config_options, mpi_config)
                try:
                    os.remove(stage4_tmp_nc)
                except OSError:
                    config_options.errMsg = f"Unable to remove temporary file: {stage4_tmp_nc}"
                    err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Create a temporary NetCDF file from the GRIB2 file.
        if(WGRIB2_env):
            cmd = f'$WGRIB2 -match "APCP:surface:0-1 hour acc fcst" {supplemental_precip.file_in2} -netcdf {stage4_tmp_nc}'
        else:
            cmd = "APCP:surface:0-1 hour acc fcst"

        if mpi_config.rank == 0:
            config_options.statusMsg = f"WGRIB2 command: {cmd}"
            err_handler.log_msg(config_options, mpi_config)
        id_tmp = ioMod.open_grib2(supplemental_precip.file_in2, stage4_tmp_nc, cmd,
                                  config_options, mpi_config, inputVar=None, special_case=False)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        create_link("STAGEIV-PCP", supplemental_precip.file_in2, stage4_tmp_nc, config_options, mpi_config)
        id_tmp = ioMod.open_netcdf_forcing(stage4_tmp_nc, config_options, mpi_config, False, lat_var, lon_var)

    # Check to see if we need to calculate regridding weights.
    calc_regrid_flag = check_supp_pcp_regrid_status(id_tmp, supplemental_precip, config_options,
                                                    wrf_hydro_geo_meta, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    if calc_regrid_flag:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Calculating STAGE IV regridding weights."
            err_handler.log_msg(config_options, mpi_config)
        calculate_supp_pcp_weights(supplemental_precip, id_tmp, stage4_tmp_nc, config_options, mpi_config, lat_var, lon_var)
        err_handler.check_program_status(config_options, mpi_config)

    if(config_options.grid_type == "gridded"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding STAGE IV '{supplemental_precip.netcdf_var_names[-1]}' Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables[supplemental_precip.netcdf_var_names[-1]][0,:,:]
                var_tmp = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,var_tmp)
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from STAGE IV file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)
    elif(config_options.grid_type == "unstructured"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding STAGE IV '{supplemental_precip.netcdf_var_names[-1]}' Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables[supplemental_precip.netcdf_var_names[-1]][0,:,:].data
                var_tmp = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,var_tmp)
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from STAGE IV file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        # Regrid the input variables.
        var_tmp_elem = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding STAGE IV '{supplemental_precip.netcdf_var_names[-1]}' Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp_elem = id_tmp.variables[supplemental_precip.netcdf_var_names[-1]][0,:,:].data
                var_tmp_elem = np.where(var_tmp_elem==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,var_tmp_elem)
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from STAGE IV file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp_elem = mpi_config.scatter_array(supplemental_precip, var_tmp_elem, config_options)
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "hydrofabric"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding STAGE IV '{supplemental_precip.netcdf_var_names[-1]}' Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables[supplemental_precip.netcdf_var_names[-1]][0,:,:]
                var_tmp = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,var_tmp)
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from STAGE IV file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

    if(config_options.grid_type == "gridded"):
        try:
            supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place STAGE IV precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid STAGE IV supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:, :] = supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the 6-hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
            supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:, :] = \
                supplemental_precip.regridded_precip2[:, :]
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "unstructured"):
        try:
            supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place STAGE IV precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid STAGE IV supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)
    
        supplemental_precip.regridded_precip2[:] = supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the 6-hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
            supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:] = \
                supplemental_precip.regridded_precip2[:]
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place STAGE IV precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out_elem = supplemental_precip.regridObj_elem(supplemental_precip.esmf_field_in_elem,
                                                                               supplemental_precip.esmf_field_out_elem)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid STAGE IV supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out_elem.data[np.where(supplemental_precip.regridded_mask_elem == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2_elem[:] = supplemental_precip.esmf_field_out_elem.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the 6-hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2_elem != config_options.globalNdv)
            supplemental_precip.regridded_precip2_elem[ind_valid] = supplemental_precip.regridded_precip2_elem[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1_elem[:] = \
                supplemental_precip.regridded_precip2_elem[:]
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "hydrofabric"):
        try:
            supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place STAGE IV precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid STAGE IV supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:] = supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)
        # Convert the 6-hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2 != 9.999e+20)#config_options.globalNdv)
            supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on STAGE IV supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)
        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:] = \
                supplemental_precip.regridded_precip2[:]
        err_handler.check_program_status(config_options, mpi_config)

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + stage4_tmp_nc
            err_handler.log_critical(config_options, mpi_config)
        try:
            os.remove(stage4_tmp_nc)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + stage4_tmp_nc
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

def regrid_conus_ext_ana_pcp(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of CONUS  ExtAna supplemental precip data.
    :param supplemental_precip:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """

    if supplemental_precip.ext_ana == "STAGE4":
        supplemental_precip.netcdf_var_names.append('APCP_surface')
        #supplemental_precip.netcdf_var_names.append('A_PCP_GDS5_SFC_acc6h')
        _regrid_conus_ext_ana_pcp_stage4(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config)
        supplemental_precip.netcdf_var_names.pop()
    else: #MRMS
        supplemental_precip.netcdf_var_names.append('MultiSensorQPE01H_0mabovemeansealevel')
        #supplemental_precip.netcdf_var_names.append('A_PCP_GDS5_SFC_acc6h')
        regrid_mrms_hourly(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config)
        supplemental_precip.netcdf_var_names.pop()


def regrid_conus_hrrr(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of HRRR data.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in2):
        if mpi_config.rank == 0:
            config_options.statusMsg = "No HRRR in_2 file found for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        err_handler.log_msg(config_options, mpi_config)
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No HRRR regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        err_handler.log_msg(config_options, mpi_config)
        return

    # Create a path for a temporary NetCDF file
    input_forcings.tmpFile = config_options.scratch_dir + "/" + "HRRR_TMP-{}.nc".format(mkfilename())
    if input_forcings.fileType != NETCDF:

        # This file shouldn't exist.... but if it does (previously failed
        # execution of the program), remove it.....
        if mpi_config.rank == 0:
            if os.path.isfile(input_forcings.tmpFile):
                config_options.statusMsg = "Found old temporary file: " + input_forcings.tmpFile + " - Removing....."
                err_handler.log_warning(config_options, mpi_config)
                try:
                    os.remove(input_forcings.tmpFile)
                except OSError:
                    config_options.errMsg = "Unable to remove temporary file: " + input_forcings.tmpFile
                    err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        fields = []
        for force_count, grib_var in enumerate(input_forcings.grib_vars):
            if mpi_config.rank == 0:
                config_options.statusMsg = "Converting HRRR Variable: " + grib_var
                err_handler.log_msg(config_options, mpi_config)
            if 0 < input_forcings.cycleFreq < 60:
                time_str = "{}-{} min acc fcst".format(input_forcings.fcst_min1, input_forcings.fcst_min2) \
                    if grib_var == 'APCP' else str(input_forcings.fcst_min2) + " min fcst"
                sub_rem = int(input_forcings.fcst_min1) % 60
                sub_id = int(sub_rem / input_forcings.cycleFreq)
            else:
                time_str = "{}-{} hour acc fcst".format(input_forcings.fcst_hour1, input_forcings.fcst_hour2) \
                    if grib_var == 'APCP' else str(input_forcings.fcst_hour2) + " hour fcst"
            fields.append(':' + grib_var + ':' +
                          input_forcings.grib_levels[force_count] + ':'
                          + time_str + ":")
        fields.append(":(HGT):(surface):")

        # Create a temporary NetCDF file from the GRIB2 file.
        if(WGRIB2_env):
            cmd = '$WGRIB2 -match "(' + '|'.join(fields) + ')" ' + input_forcings.file_in2 + \
                  " -netcdf " + input_forcings.tmpFile
        else:
            cmd = '(' + '|'.join(fields) + ')'

        id_tmp = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFile, cmd,
                                  config_options, mpi_config, inputVar=None, special_case=False)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        create_link("HRRR", input_forcings.file_in2, input_forcings.tmpFile, config_options, mpi_config)
        id_tmp = ioMod.open_netcdf_forcing(input_forcings.tmpFile, config_options, mpi_config)

    for force_count, grib_var in enumerate(input_forcings.grib_vars):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing HRRR Variable: " + grib_var
            err_handler.log_msg(config_options, mpi_config)

        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if calc_regrid_flag:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Calculating HRRR regridding weights."
                err_handler.log_msg(config_options, mpi_config)
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)
            err_handler.check_program_status(config_options, mpi_config)

            # # Read in the HRRR height field, which is used for downscaling purposes.
            # if mpi_config.rank == 0:
            #     config_options.statusMsg = "Reading in HRRR elevation data."
            #     err_handler.log_msg(config_options, mpi_config)
            # cmd = "$WGRIB2 " + input_forcings.file_in2 + " -match " + \
            #       "\":(HGT):(surface):\" " + \
            #       " -netcdf " + input_forcings.tmpFileHeight
            # id_tmp_height = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFileHeight,
            #                                  cmd, config_options, mpi_config, 'HGT_surface')
            # err_handler.check_program_status(config_options, mpi_config)

            if(config_options.grid_type == "gridded"):
                # Regrid the height variable.
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        if 0 < input_forcings.cycleFreq < 60:
                            var_tmp = id_tmp.variables['HGT_surface'][sub_id]
                        else:
                            var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HRRR elevation from " + \
                                                input_forcings.tmpFile + ": " + str(err)

                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place input NetCDF HRRR data into the ESMF field object: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding HRRR surface elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid HRRR surface elevation using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform HRRR mask search on elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:, :] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract regridded HRRR elevation data from ESMF: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "unstructured"):
                # Regrid the height variable.
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        if 0 < input_forcings.cycleFreq < 60:
                            var_tmp = id_tmp.variables['HGT_surface'][sub_id]
                        else:
                            var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HRRR elevation from " + \
                                                input_forcings.tmpFile + ": " + str(err)

                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place input NetCDF HRRR data into the ESMF field object: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding HRRR surface elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid HRRR surface elevation using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform HRRR mask search on elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract regridded HRRR elevation data from ESMF: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Regrid the height variable.
                var_tmp_elem = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp_elem = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HRRR elevation from " + \
                                                input_forcings.tmpFile + ": " + str(err)

                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place input NetCDF HRRR data into the ESMF field object: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding HRRR surface elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                             input_forcings.esmf_field_out_elem)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid HRRR surface elevation using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform HRRR mask search on elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height_elem[:] = input_forcings.esmf_field_out_elem.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract regridded HRRR elevation data from ESMF: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "hydrofabric"):
                # Regrid the height variable.
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        if 0 < input_forcings.cycleFreq < 60:
                            var_tmp = id_tmp.variables['HGT_surface'][sub_id]
                        else:
                            var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HRRR elevation from " + \
                                                input_forcings.tmpFile + ": " + str(err)

                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place input NetCDF HRRR data into the ESMF field object: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding HRRR surface elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid HRRR surface elevation using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform HRRR mask search on elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract regridded HRRR elevation data from ESMF: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # Close the temporary NetCDF file and remove it.
            # if mpi_config.rank == 0:
            #     try:
            #         id_tmp_height.close()
            #     except OSError:
            #         config_options.errMsg = "Unable to close temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
            #
            #     try:
            #         os.remove(input_forcings.tmpFileHeight)
            #     except OSError:
            #         config_options.errMsg = "Unable to remove temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Processing input HRRR variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    if 0 < input_forcings.cycleFreq < 60:
                        var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][sub_id, :, :]
                    else:
                        var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                    if grib_var == "APCP":
                        var_tmp /= 3600     # convert hourly accumulated precip to instantaneous rate
                    if grib_var == 'CPOFP':
                        var_tmp[var_tmp >=0] = (100 - var_tmp[var_tmp >=0]) / 100  # convert frozen fraction to liquid fraction
                        var_tmp[var_tmp < 0] = 1.0                                 # assume all liquid if not specifically given
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place input HRRR data into ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input HRRR Field: " + input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input HRRR forcing data: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to perform mask test on regridded HRRR forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract regridded HRRR forcing data from the ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            # mpi_config.comm.barrier()

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Processing input HRRR variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    if 0 < input_forcings.cycleFreq < 60:
                        var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][sub_id, :, :]
                    else:
                        var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                    if grib_var == "APCP":
                        var_tmp /= 3600     # convert hourly accumulated precip to instantaneous rate
                    if grib_var == 'CPOFP':
                        var_tmp[var_tmp >=0] = (100 - var_tmp[var_tmp >=0]) / 100  # convert frozen fraction to liquid fraction
                        var_tmp[var_tmp < 0] = 1.0                                 # assume all liquid if not specifically given
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place input HRRR data into ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input HRRR Field: " + input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input HRRR forcing data: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to perform mask test on regridded HRRR forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract regridded HRRR forcing data from the ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            # mpi_config.comm.barrier()

            # Regrid the input variables.
            var_tmp_elem = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Processing input HRRR variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    if 0 < input_forcings.cycleFreq < 60:
                        var_tmp_elem = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][sub_id, :, :]
                    else:
                        var_tmp_elem = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                    if grib_var == "APCP":
                        var_tmp_elem /= 3600     # convert hourly accumulated precip to instantaneous rate
                    if grib_var == 'CPOFP':
                        var_tmp_elem[var_tmp_elem >=0] = (100 - var_tmp_elem[var_tmp_elem >=0]) / 100  # convert frozen fraction to liquid fraction
                        var_tmp_elem[var_tmp_elem < 0] = 1.0                                 # assume all liquid if not specifically given
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place input HRRR data into ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input HRRR Field: " + input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input HRRR forcing data: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to perform mask test on regridded HRRR forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out_elem.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract regridded HRRR forcing data from the ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            # mpi_config.comm.barrier()

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Processing input HRRR variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    if 0 < input_forcings.cycleFreq < 60:
                        var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][sub_id, :, :]
                    else:
                        var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                    if grib_var == "APCP":
                        var_tmp /= 3600     # convert hourly accumulated precip to instantaneous rate
                    if grib_var == 'CPOFP':
                        var_tmp[var_tmp >=0] = (100 - var_tmp[var_tmp >=0]) / 100  # convert frozen fraction to liquid fraction
                        var_tmp[var_tmp < 0] = 1.0                                 # assume all liquid if not specifically given
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place input HRRR data into ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input HRRR Field: " + input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input HRRR forcing data: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to perform mask test on regridded HRRR forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract regridded HRRR forcing data from the ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            # mpi_config.comm.barrier()

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
        try:
            os.remove(input_forcings.tmpFile)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
    # mpi_config.comm.barrier()


def regrid_conus_rap(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of RAP 13km conus data.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in2):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No RAP regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return

    # Create a path for a temporary NetCDF file
    input_forcings.tmpFile = config_options.scratch_dir + "/" + "RAP_CONUS_TMP-{}.nc".format(mkfilename())
    err_handler.check_program_status(config_options, mpi_config)

    if input_forcings.fileType != NETCDF:
        # This file shouldn't exist.... but if it does (previously failed
        # execution of the program), remove it.....
        if mpi_config.rank == 0:
            if os.path.isfile(input_forcings.tmpFile):
                config_options.statusMsg = "Found old temporary file: " + \
                                           input_forcings.tmpFile + " - Removing....."
                err_handler.log_warning(config_options, mpi_config)
                try:
                    os.remove(input_forcings.tmpFile)
                except OSError:
                    config_options.errMsg = "Unable to remove file: " + input_forcings.tmpFile
                    err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        fields = []
        for force_count, grib_var in enumerate(input_forcings.grib_vars):
            if mpi_config.rank == 0:
                config_options.statusMsg = "Converting CONUS RAP Variable: " + grib_var
                err_handler.log_msg(config_options, mpi_config)
            time_str = "{}-{} hour acc fcst".format(input_forcings.fcst_hour1, input_forcings.fcst_hour2) \
                if grib_var in ("APCP", "FROZR") else str(input_forcings.fcst_hour2) + " hour fcst"
            fields.append(':' + grib_var + ':' +
                          input_forcings.grib_levels[force_count] + ':'
                          + time_str + ":")
        fields.append(":(HGT):(surface):")

        # Create a temporary NetCDF file from the GRIB2 file.
        if(WGRIB2_env):
            cmd = '$WGRIB2 -match "(' + '|'.join(fields) + ')" ' + input_forcings.file_in2 + \
                  " -netcdf " + input_forcings.tmpFile
        else:
            cmd = '(' + '|'.join(fields) + ')'

        id_tmp = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFile, cmd,
                                  config_options, mpi_config, inputVar=None, special_case=False)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        create_link("RAP", input_forcings.file_in2, input_forcings.tmpFile, config_options, mpi_config)
        id_tmp = ioMod.open_netcdf_forcing(input_forcings.tmpFile, config_options, mpi_config)

    for force_count, grib_var in enumerate(input_forcings.grib_vars):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing Conus RAP Variable: " + grib_var
            err_handler.log_msg(config_options, mpi_config)

        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if calc_regrid_flag:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Calculating RAP regridding weights."
                err_handler.log_msg(config_options, mpi_config)
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)
            err_handler.check_program_status(config_options, mpi_config)

            # Read in the RAP height field, which is used for downscaling purposes.
            # if mpi_config.rank == 0:
            #     config_options.statusMsg = "Reading in RAP elevation data."
            #     err_handler.log_msg(config_options, mpi_config)
            # cmd = "$WGRIB2 " + input_forcings.file_in2 + " -match " + \
            #       "\":(HGT):(surface):\" " + \
            #       " -netcdf " + input_forcings.tmpFileHeight
            # id_tmp_height = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFileHeight,
            #                                  cmd, config_options, mpi_config, 'HGT_surface')
            # err_handler.check_program_status(config_options, mpi_config)
            if(config_options.grid_type == "gridded"):
                # Regrid the height variable.
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HGT_surface from : " + id_tmp + \
                                                " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place temporary RAP elevation variable into ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding RAP surface elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid RAP elevation data using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform mask search on RAP elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:, :] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place RAP ESMF elevation field into local array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "unstructured"):
                # Regrid the height variable.
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HGT_surface from : " + id_tmp + \
                                                " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place temporary RAP elevation variable into ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding RAP surface elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid RAP elevation data using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform mask search on RAP elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place RAP ESMF elevation field into local array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Regrid the height variable.
                var_tmp_elem = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp_elem = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HGT_surface from : " + id_tmp + \
                                                " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place temporary RAP elevation variable into ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding RAP surface elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid RAP elevation data using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform mask search on RAP elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height_elem[:] = input_forcings.esmf_field_out_elem.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place RAP ESMF elevation field into local array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "hydrofabric"):
                # Regrid the height variable.
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HGT_surface from : " + id_tmp + \
                                                " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place temporary RAP elevation variable into ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding RAP surface elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid RAP elevation data using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform mask search on RAP elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place RAP ESMF elevation field into local array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # Close the temporary NetCDF file and remove it.
            # if mpi_config.rank == 0:
            #     try:
            #         id_tmp_height.close()
            #     except OSError:
            #         config_options.errMsg = "Unable to close temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
            #
            #     try:
            #         os.remove(input_forcings.tmpFileHeight)
            #     except OSError:
            #         config_options.errMsg = "Unable to remove temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
            # err_handler.check_program_status(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                    if grib_var in ("APCP", "FROZR"):
                        var_tmp /= 3600     # convert hourly accumulated precip to instantaneous rate
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + \
                                            " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local RAP array into ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input RAP Field: " + input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid RAP variable: " + input_forcings.netcdf_var_names[force_count] \
                                        + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask calculation on RAP variable: " + \
                                        input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if force_count < 8:
                try:
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                        input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place RAP ESMF data into local array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)
            else:
                # handle liquid-phase precip calculation
                RAINRATE = 3  # TODO: determine this programmatically
                total_pcp = np.ma.masked_values(input_forcings.regridded_forcings2[RAINRATE], config_options.globalNdv)
                frozn_pcp = np.ma.masked_values(input_forcings.esmf_field_out.data, config_options.globalNdv)
                # print(f"rank {mpi_config.rank} has {(frozn_pcp > total_pcp).sum()} instances of frozn_pcp > total_pcp", flush=True)
                frz_fract = frozn_pcp / total_pcp
                frz_fract[frz_fract > 1] = 1
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = (1 - frz_fract).filled(1.0)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                    if grib_var in ("APCP", "FROZR"):
                        var_tmp /= 3600     # convert hourly accumulated precip to instantaneous rate
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + \
                                            " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local RAP array into ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input RAP Field: " + input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid RAP variable: " + input_forcings.netcdf_var_names[force_count] \
                                        + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask calculation on RAP variable: " + \
                                        input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if force_count < 8:
                try:
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                        input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place RAP ESMF data into local array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)
            else:
                # handle liquid-phase precip calculation
                RAINRATE = 3  # TODO: determine this programmatically
                total_pcp = np.ma.masked_values(input_forcings.regridded_forcings2[RAINRATE], config_options.globalNdv)
                frozn_pcp = np.ma.masked_values(input_forcings.esmf_field_out.data, config_options.globalNdv)
                # print(f"rank {mpi_config.rank} has {(frozn_pcp > total_pcp).sum()} instances of frozn_pcp > total_pcp", flush=True)
                frz_fract = frozn_pcp / total_pcp
                frz_fract[frz_fract > 1] = 1
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = (1 - frz_fract).filled(1.0)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

            # Regrid the input variables.
            var_tmp_elem = None
            if mpi_config.rank == 0:
                try:
                    var_tmp_elem = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                    if grib_var in ("APCP", "FROZR"):
                        var_tmp_elem /= 3600     # convert hourly accumulated precip to instantaneous rate
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + \
                                            " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local RAP array into ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input RAP Field: " + input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid RAP variable: " + input_forcings.netcdf_var_names[force_count] \
                                        + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask calculation on RAP variable: " + \
                                        input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if force_count < 8:
                try:
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                        input_forcings.esmf_field_out_elem.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place RAP ESMF element data into local array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)
            else:
                # handle liquid-phase precip calculation
                RAINRATE = 3  # TODO: determine this programmatically
                total_pcp = np.ma.masked_values(input_forcings.regridded_forcings2_elem[RAINRATE], config_options.globalNdv)
                frozn_pcp = np.ma.masked_values(input_forcings.esmf_field_out_elem.data, config_options.globalNdv)
                # print(f"rank {mpi_config.rank} has {(frozn_pcp > total_pcp).sum()} instances of frozn_pcp > total_pcp", flush=True)
                frz_fract = frozn_pcp / total_pcp
                frz_fract[frz_fract > 1] = 1
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = (1 - frz_fract).filled(1.0)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                    if grib_var in ("APCP", "FROZR"):
                        var_tmp /= 3600     # convert hourly accumulated precip to instantaneous rate
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + \
                                            " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local RAP array into ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input RAP Field: " + input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid RAP variable: " + input_forcings.netcdf_var_names[force_count] \
                                        + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask calculation on RAP variable: " + \
                                        input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if force_count < 8:
                try:
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                        input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place RAP ESMF data into local array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)
            else:
                # handle liquid-phase precip calculation
                RAINRATE = 3  # TODO: determine this programmatically
                total_pcp = np.ma.masked_values(input_forcings.regridded_forcings2[RAINRATE], config_options.globalNdv)
                frozn_pcp = np.ma.masked_values(input_forcings.esmf_field_out.data, config_options.globalNdv)
                # print(f"rank {mpi_config.rank} has {(frozn_pcp > total_pcp).sum()} instances of frozn_pcp > total_pcp", flush=True)
                frz_fract = frozn_pcp / total_pcp
                frz_fract[frz_fract > 1] = 1
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = (1 - frz_fract).filled(1.0)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
        try:
            os.remove(input_forcings.tmpFile)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)


def regrid_cfsv2(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of global CFSv2 forecast data.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in2):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        # Check to see if we are running NWM-custom interpolation/bias
        # correction on incoming CFSv2 data. Because of the nature, we
        # need to regrid bias-corrected data every hour.
        if mpi_config.rank == 0:
            config_options.statusMsg = "No need to read in new CFSv2 data at this time."
            err_handler.log_msg(config_options, mpi_config)
        return

    # Create a path for a temporary NetCDF file
    input_forcings.tmpFile = config_options.scratch_dir + "/" + "CFSv2_TMP-{}.nc".format(mkfilename())
    err_handler.check_program_status(config_options, mpi_config)

    if input_forcings.fileType != NETCDF:

        # This file shouldn't exist.... but if it does (previously failed
        # execution of the program), remove it.....
        if mpi_config.rank == 0:
            if os.path.isfile(input_forcings.tmpFile):
                config_options.statusMsg = "Found old temporary file: " + \
                                           input_forcings.tmpFile + " - Removing....."
                err_handler.log_warning(config_options, mpi_config)
                try:
                    os.remove(input_forcings.tmpFile)
                except OSError as err:
                    config_options.errMsg = "Unable to remove previous temporary file: " \
                                            + input_forcings.tmpFile + str(err)
                    err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        fields = []
        for force_count, grib_var in enumerate(input_forcings.grib_vars):
            if mpi_config.rank == 0:
                config_options.statusMsg = "Converting CFSv2 Variable: " + grib_var
                err_handler.log_msg(config_options, mpi_config)
            fields.append(':' + grib_var + ':' +
                          input_forcings.grib_levels[force_count] + ':'
                          + str(input_forcings.fcst_hour2) + " hour fcst:")
        fields.append(":(HGT):(surface):")

        # Create a temporary NetCDF file from the GRIB2 file.
        if(WGRIB2_env):
            cmd = '$WGRIB2 -match "(' + '|'.join(fields) + ')" ' + input_forcings.file_in2 + \
                  " -netcdf " + input_forcings.tmpFile
        else:
            cmd = '(' + '|'.join(fields) + ')'

        id_tmp = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFile, cmd,
                                  config_options, mpi_config, inputVar=None, special_case=False)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        create_link("CFSv2", input_forcings.file_in2, input_forcings.tmpFile, config_options, mpi_config)
        id_tmp = ioMod.open_netcdf_forcing(input_forcings.tmpFile, config_options, mpi_config)

    for force_count, grib_var in enumerate(input_forcings.grib_vars):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing CFSv2 Variable: " + grib_var
            err_handler.log_msg(config_options, mpi_config)

        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if calc_regrid_flag:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Calculate CFSv2 regridding weights."
                err_handler.log_msg(config_options, mpi_config)

            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)
            err_handler.check_program_status(config_options, mpi_config)

            # Read in the RAP height field, which is used for downscaling purposes.
            # if mpi_config.rank == 0:
            #     config_options.statusMsg = "Reading in CFSv2 elevation data."
            #     err_handler.log_msg(config_options, mpi_config)
            #
            # cmd = "$WGRIB2 " + input_forcings.file_in2 + " -match " + \
            #       "\":(HGT):(surface):\" " + \
            #       " -netcdf " + input_forcings.tmpFileHeight
            # id_tmp_height = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFileHeight,
            #                                  cmd, config_options, mpi_config, 'HGT_surface')
            # err_handler.check_program_status(config_options, mpi_config)

            if(config_options.grid_type == "gridded"):
                # Regrid the height variable.
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HGT_surface from file: " \
                                                + input_forcings.file_in2 + " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place CFSv2 elevation data into the ESMF field object: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding CFSv2 elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid CFSv2 elevation data to the WRF-Hydro domain: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to run mask calculation on CFSv2 elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:, :] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract CFSv2 regridded elevation data from ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "unstructured"):
                # Regrid the height variable.
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HGT_surface from file: " \
                                                + input_forcings.file_in2 + " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place CFSv2 elevation data into the ESMF field object: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding CFSv2 elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid CFSv2 elevation data to the WRF-Hydro domain: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to run mask calculation on CFSv2 elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract CFSv2 regridded elevation data from ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Regrid the height variable.
                var_tmp_elem = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp_elem = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HGT_surface from file: " \
                                                + input_forcings.file_in2 + " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place CFSv2 elevation data into the ESMF field object: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding CFSv2 elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                             input_forcings.esmf_field_out_elem)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid CFSv2 elevation data to the WRF-Hydro domain: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to run mask calculation on CFSv2 elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height_elem[:] = input_forcings.esmf_field_out_elem.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract CFSv2 regridded elevation data from ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "hydrofabric"):
                # Regrid the height variable.
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract HGT_surface from file: " \
                                                + input_forcings.file_in2 + " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place CFSv2 elevation data into the ESMF field object: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding CFSv2 elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid CFSv2 elevation data to the WRF-Hydro domain: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to run mask calculation on CFSv2 elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract CFSv2 regridded elevation data from ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # Close the temporary NetCDF file and remove it.
            # if mpi_config.rank == 0:
            #     try:
            #         id_tmp_height.close()
            #     except OSError:
            #         config_options.errMsg = "Unable to close temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
            # err_handler.check_program_status(config_options, mpi_config)
            #
            # if mpi_config.rank == 0:
            #     try:
            #         os.remove(input_forcings.tmpFileHeight)
            #     except OSError:
            #         config_options.errMsg = "Unable to remove temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
            # err_handler.check_program_status(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                if not config_options.runCfsNldasBiasCorrect:
                    config_options.statusMsg = "Regridding CFSv2 variable: " + \
                                               input_forcings.netcdf_var_names[force_count]
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from file: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Scatter the global CFSv2 data to the local processors.
            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            # Assign local CFSv2 data to the input forcing object.. IF..... we are running the
            # bias correction. These grids are interpolated in a separate routine, AFTER bias
            # correction has taken place.
            if config_options.runCfsNldasBiasCorrect:
                if input_forcings.coarse_input_forcings1 is None:  # and config_options.current_output_step == 1:
                    # if not np.any(input_forcings.coarse_input_forcings1) and not \
                    #        np.any(input_forcings.coarse_input_forcings2) and \
                    #        ConfigOptions.current_output_step == 1:
                    # We need to create NumPy arrays to hold the CFSv2 global data.
                    input_forcings.coarse_input_forcings1 = np.empty([9, var_sub_tmp.shape[0], var_sub_tmp.shape[1]],
                                                                     np.float64)

                if input_forcings.coarse_input_forcings2 is None:  # and config_options.current_output_step == 1:
                    # if not np.any(input_forcings.coarse_input_forcings1) and not \
                    #        np.any(input_forcings.coarse_input_forcings2) and \
                    #        ConfigOptions.current_output_step == 1:
                    # We need to create NumPy arrays to hold the CFSv2 global data.
                    input_forcings.coarse_input_forcings2 = np.empty([9, var_sub_tmp.shape[0], var_sub_tmp.shape[1]],
                                                                     np.float64)

                try:
                    input_forcings.coarse_input_forcings2[input_forcings.input_map_output[force_count], :, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place local CFSv2 input variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + \
                                            " into local numpy array. (" + str(err) + ")"
                # except TypeError:
                #    print("DEBUG: ", input_forcings.coarse_input_forcings2, input_forcings.input_map_output, force_count)

                if config_options.current_output_step == 1:
                    input_forcings.coarse_input_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                        input_forcings.coarse_input_forcings2[input_forcings.input_map_output[force_count], :, :]
            else:
                input_forcings.coarse_input_forcings2 = None
                input_forcings.coarse_input_forcings1 = None
            err_handler.check_program_status(config_options, mpi_config)

            # Only regrid the current files if we did not specify the NLDAS2 NWM bias correction, which needs to take place
            # first before any regridding can take place. That takes place in the bias-correction routine.
            if not config_options.runCfsNldasBiasCorrect:
                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place CFSv2 forcing data into temporary ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid CFSv2 variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + " (" + str(ve) + ")"
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to run mask calculation on CFSv2 variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                        input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF field data for CFSv2: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # If we are on the first timestep, set the previous regridded field to be
                # the latest as there are no states for time 0.
                if config_options.current_output_step == 1:
                    input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                        input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
                err_handler.check_program_status(config_options, mpi_config)
            else:
                # Set regridded arrays to dummy values as they are regridded later in the bias correction routine.
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    config_options.globalNdv
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    config_options.globalNdv

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                if not config_options.runCfsNldasBiasCorrect:
                    config_options.statusMsg = "Regridding CFSv2 variable: " + \
                                               input_forcings.netcdf_var_names[force_count]
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from file: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Scatter the global CFSv2 data to the local processors.
            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            # Assign local CFSv2 data to the input forcing object.. IF..... we are running the
            # bias correction. These grids are interpolated in a separate routine, AFTER bias
            # correction has taken place.
            if config_options.runCfsNldasBiasCorrect:
                if input_forcings.coarse_input_forcings1 is None:  # and config_options.current_output_step == 1:
                    # if not np.any(input_forcings.coarse_input_forcings1) and not \
                    #        np.any(input_forcings.coarse_input_forcings2) and \
                    #        ConfigOptions.current_output_step == 1:
                    # We need to create NumPy arrays to hold the CFSv2 global data.
                    input_forcings.coarse_input_forcings1 = np.empty([9, var_sub_tmp.shape[0], var_sub_tmp.shape[1]],
                                                                     np.float64)

                if input_forcings.coarse_input_forcings2 is None:  # and config_options.current_output_step == 1:
                    # if not np.any(input_forcings.coarse_input_forcings1) and not \
                    #        np.any(input_forcings.coarse_input_forcings2) and \
                    #        ConfigOptions.current_output_step == 1:
                    # We need to create NumPy arrays to hold the CFSv2 global data.
                    input_forcings.coarse_input_forcings2 = np.empty([9, var_sub_tmp.shape[0], var_sub_tmp.shape[1]],
                                                                     np.float64)

                try:
                    input_forcings.coarse_input_forcings2[input_forcings.input_map_output[force_count], :, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place local CFSv2 input variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + \
                                            " into local numpy array. (" + str(err) + ")"
                # except TypeError:
                #    print("DEBUG: ", input_forcings.coarse_input_forcings2, input_forcings.input_map_output, force_count)

                if config_options.current_output_step == 1:
                    input_forcings.coarse_input_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                        input_forcings.coarse_input_forcings2[input_forcings.input_map_output[force_count], :, :]
            else:
                input_forcings.coarse_input_forcings2 = None
                input_forcings.coarse_input_forcings1 = None
            err_handler.check_program_status(config_options, mpi_config)

            # Only regrid the current files if we did not specify the NLDAS2 NWM bias correction, which needs to take place
            # first before any regridding can take place. That takes place in the bias-correction routine.
            if not config_options.runCfsNldasBiasCorrect:
                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place CFSv2 forcing data into temporary ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid CFSv2 variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + " (" + str(ve) + ")"
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to run mask calculation on CFSv2 variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                        input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF field data for CFSv2: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # If we are on the first timestep, set the previous regridded field to be
                # the latest as there are no states for time 0.
                if config_options.current_output_step == 1:
                    input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                        input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
                err_handler.check_program_status(config_options, mpi_config)
            else:
                # Set regridded arrays to dummy values as they are regridded later in the bias correction routine.
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    config_options.globalNdv
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    config_options.globalNdv

            # Regrid the input variables.
            var_tmp_elem = None
            if mpi_config.rank == 0:
                if not config_options.runCfsNldasBiasCorrect:
                    config_options.statusMsg = "Regridding CFSv2 variable: " + \
                                               input_forcings.netcdf_var_names[force_count]
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp_elem = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from file: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Scatter the global CFSv2 data to the local processors.
            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            # Assign local CFSv2 data to the input forcing object.. IF..... we are running the
            # bias correction. These grids are interpolated in a separate routine, AFTER bias
            # correction has taken place.
            if config_options.runCfsNldasBiasCorrect:
                if input_forcings.coarse_input_forcings1_elem is None:  # and config_options.current_output_step == 1:
                    # if not np.any(input_forcings.coarse_input_forcings1) and not \
                    #        np.any(input_forcings.coarse_input_forcings2) and \
                    #        ConfigOptions.current_output_step == 1:
                    # We need to create NumPy arrays to hold the CFSv2 global data.
                    input_forcings.coarse_input_forcings1_elem = np.empty([9, var_sub_tmp_elem.shape[0], var_sub_tmp_elem.shape[1]],
                                                                     np.float64)

                if input_forcings.coarse_input_forcings2_elem is None:  # and config_options.current_output_step == 1:
                    # if not np.any(input_forcings.coarse_input_forcings1) and not \
                    #        np.any(input_forcings.coarse_input_forcings2) and \
                    #        ConfigOptions.current_output_step == 1:
                    # We need to create NumPy arrays to hold the CFSv2 global data.
                    input_forcings.coarse_input_forcings2_elem = np.empty([9, var_sub_tmp_elem.shape[0], var_sub_tmp_elem.shape[1]],
                                                                     np.float64)

                try:
                    input_forcings.coarse_input_forcings2_elem[input_forcings.input_map_output[force_count], :, :] = var_sub_tmp_elem
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place local CFSv2 input variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + \
                                            " into local numpy array. (" + str(err) + ")"
                # except TypeError:
                #    print("DEBUG: ", input_forcings.coarse_input_forcings2, input_forcings.input_map_output, force_count)

                if config_options.current_output_step == 1:
                    input_forcings.coarse_input_forcings1_elem[input_forcings.input_map_output[force_count], :, :] = \
                        input_forcings.coarse_input_forcings2_elem[input_forcings.input_map_output[force_count], :, :]
            else:
                input_forcings.coarse_input_forcings2_elem = None
                input_forcings.coarse_input_forcings1_elem = None
            err_handler.check_program_status(config_options, mpi_config)

            # Only regrid the current files if we did not specify the NLDAS2 NWM bias correction, which needs to take place
            # first before any regridding can take place. That takes place in the bias-correction routine.
            if not config_options.runCfsNldasBiasCorrect:
                try:
                    input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place CFSv2 forcing data into temporary ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                             input_forcings.esmf_field_out_elem)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid CFSv2 variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + " (" + str(ve) + ")"
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to run mask calculation on CFSv2 variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                        input_forcings.esmf_field_out_elem.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF field data for CFSv2: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # If we are on the first timestep, set the previous regridded field to be
                # the latest as there are no states for time 0.
                if config_options.current_output_step == 1:
                    input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                        input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
                err_handler.check_program_status(config_options, mpi_config)
            else:
                # Set regridded arrays to dummy values as they are regridded later in the bias correction routine.
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    config_options.globalNdv
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                    config_options.globalNdv

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                if not config_options.runCfsNldasBiasCorrect:
                    config_options.statusMsg = "Regridding CFSv2 variable: " + \
                                               input_forcings.netcdf_var_names[force_count]
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from file: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Scatter the global CFSv2 data to the local processors.
            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            # Assign local CFSv2 data to the input forcing object.. IF..... we are running the
            # bias correction. These grids are interpolated in a separate routine, AFTER bias
            # correction has taken place.
            if config_options.runCfsNldasBiasCorrect:
                if input_forcings.coarse_input_forcings1 is None:  # and config_options.current_output_step == 1:
                    # if not np.any(input_forcings.coarse_input_forcings1) and not \
                    #        np.any(input_forcings.coarse_input_forcings2) and \
                    #        ConfigOptions.current_output_step == 1:
                    # We need to create NumPy arrays to hold the CFSv2 global data.
                    input_forcings.coarse_input_forcings1 = np.empty([9, var_sub_tmp.shape[0], var_sub_tmp.shape[1]],
                                                                     np.float64)

                if input_forcings.coarse_input_forcings2 is None:  # and config_options.current_output_step == 1:
                    # if not np.any(input_forcings.coarse_input_forcings1) and not \
                    #        np.any(input_forcings.coarse_input_forcings2) and \
                    #        ConfigOptions.current_output_step == 1:
                    # We need to create NumPy arrays to hold the CFSv2 global data.
                    input_forcings.coarse_input_forcings2 = np.empty([9, var_sub_tmp.shape[0], var_sub_tmp.shape[1]],
                                                                     np.float64)

                try:
                    input_forcings.coarse_input_forcings2[input_forcings.input_map_output[force_count], :, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place local CFSv2 input variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + \
                                            " into local numpy array. (" + str(err) + ")"
                # except TypeError:
                #    print("DEBUG: ", input_forcings.coarse_input_forcings2, input_forcings.input_map_output, force_count)

                if config_options.current_output_step == 1:
                    input_forcings.coarse_input_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                        input_forcings.coarse_input_forcings2[input_forcings.input_map_output[force_count], :, :]
            else:
                input_forcings.coarse_input_forcings2 = None
                input_forcings.coarse_input_forcings1 = None
            err_handler.check_program_status(config_options, mpi_config)

            # Only regrid the current files if we did not specify the NLDAS2 NWM bias correction, which needs to take place
            # first before any regridding can take place. That takes place in the bias-correction routine.
            if not config_options.runCfsNldasBiasCorrect:
                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place CFSv2 forcing data into temporary ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid CFSv2 variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + " (" + str(ve) + ")"
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to run mask calculation on CFSv2 variable: " + \
                                            input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                        input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF field data for CFSv2: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # If we are on the first timestep, set the previous regridded field to be
                # the latest as there are no states for time 0.
                if config_options.current_output_step == 1:
                    input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                        input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
                err_handler.check_program_status(config_options, mpi_config)
            else:
                # Set regridded arrays to dummy values as they are regridded later in the bias correction routine.
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    config_options.globalNdv
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    config_options.globalNdv

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    if mpi_config.rank == 0:
        try:
            os.remove(input_forcings.tmpFile)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

def regrid_nwm(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of custom input NetCDF hourly forcing files.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """

    ## Flag to jump to different regridding module for AWS NWM Forcing data
    if(config_options.aws):
        regrid_nwm_aws(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config)
        return

    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in2):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No NWM NetCDF regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return

    # Open the input NetCDF file containing necessary data.
    id_tmp = ioMod.open_netcdf_forcing(input_forcings.file_in2, config_options, mpi_config, open_on_all_procs=True)

    for force_count, nc_var in enumerate(input_forcings.netcdf_var_names):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing Custom NetCDF Forcing Variable: " + nc_var
            err_handler.log_msg(config_options, mpi_config)
        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)

        if calc_regrid_flag:
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)

        input_forcings.height = None
        if mpi_config.rank == 0:
            config_options.statusMsg = f"Unable to locate HGT_surface in: {input_forcings.file_in2}. " \
                                       f"Downscaling will not be available."
            err_handler.log_msg(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[nc_var][:][0, :, :]
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[nc_var][:][0, :, :]
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

            # Regrid the input variables.
            var_tmp_elem = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp_elem = id_tmp.variables[nc_var][:][0, :, :]
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out_elem.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[nc_var][:][0, :, :]
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)
          
            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)
    # Close the NetCDF file
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.err_out(config_options)

def regrid_nwm_aws(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of AWS NWM Forcing Data Downloaded from Server.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No NWM NetCDF regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return


    mpi_config.comm.barrier()
    with MPICommExecutor(comm=mpi_config.comm, root=0) as executor:
        with dask.config.set(scheduler=executor):
            if mpi_config.rank == 0:
                id_tmp = config_options.aws_obj.sel(time=config_options.current_time.strftime('%Y-%m-%d %H:%M:%S'))
                id_tmp = id_tmp.compute()
            else:
                id_tmp = None
    mpi_config.comm.barrier()


    for force_count, nc_var in enumerate(input_forcings.netcdf_var_names):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing Custom NetCDF Forcing Variable: " + nc_var
            err_handler.log_msg(config_options, mpi_config)
        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)

        if calc_regrid_flag:
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)

        input_forcings.height = None
        if mpi_config.rank == 0:
            config_options.statusMsg = f"Unable to locate HGT_surface in: {input_forcings.file_in2}. " \
                                       f"Downscaling will not be available."
            err_handler.log_msg(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp[nc_var].to_masked_array()
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp[nc_var].to_masked_array()
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

            # Regrid the input variables.
            var_tmp_elem = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp_elem = id_tmp[nc_var].to_masked_array()
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out_elem.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp[nc_var].to_masked_array()
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)
    # Close the NetCDF file
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.err_out(config_options)



def regrid_custom_hourly_netcdf(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of custom input NetCDF hourly forcing files.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
   
    # Flag to jump to different regridding function soley for AORC AWS data
    if(config_options.aws):
        regrid_aorc_aws(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config)
        return

    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in2):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No Custom Hourly NetCDF regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return

    # Open the input NetCDF file containing necessary data.
    id_tmp = ioMod.open_netcdf_forcing(input_forcings.file_in2, config_options, mpi_config, open_on_all_procs=True)

    fill_values = {'TMP': 288.0, 'SPFH': 0.005, 'PRES': 101300.0, 'APCP': 0,
                   'UGRD': 1.0, 'VGRD': 1.0, 'DSWRF': 80.0, 'DLWRF': 310.0}

    for force_count, nc_var in enumerate(input_forcings.netcdf_var_names):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing Custom NetCDF Forcing Variable: " + nc_var
            err_handler.log_msg(config_options, mpi_config)
        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)
            
        if calc_regrid_flag:
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)

            # Flag to set regridded mask for AORC to overlay with ERA5-Interim blend
            if(23 in config_options.input_forcings):
                input_forcings.regridded_mask_AORC = input_forcings.regridded_mask
                if(config_options.grid_type == "unstructured"):
                    input_forcings.regridded_mask_elem_AORC = input_forcings.regridded_mask_elem

            # Read in the RAP height field, which is used for downscaling purposes.
            if 'HGT_surface' in id_tmp.variables.keys():
                if(config_options.grid_type == "gridded"):
                    # Regrid the height variable.
                    if mpi_config.rank == 0:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    else:
                        var_tmp = None
                    err_handler.check_program_status(config_options, mpi_config)

                    var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                    err_handler.check_program_status(config_options, mpi_config)

                    try:
                        input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to place NetCDF elevation data into the ESMF field object: " \
                                                + str(err)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    if mpi_config.rank == 0:
                        config_options.statusMsg = "Regridding elevation data to the WRF-Hydro domain."
                        err_handler.log_msg(config_options, mpi_config)
                    try:
                        input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                    except ValueError as ve:
                        config_options.errMsg = "Unable to regrid elevation data to the WRF-Hydro domain " \
                                                "using ESMF: " + str(ve)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    # Set any pixel cells outside the input domain to the global missing value.
                    try:
                        input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                            config_options.globalNdv
                    except (ValueError, ArithmeticError) as npe:
                        config_options.errMsg = "Unable to compute mask on elevation data: " + str(npe)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    try:
                        input_forcings.height[:, :] = input_forcings.esmf_field_out.data
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract ESMF regridded elevation data to a local " \
                                                "array: " + str(err)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                elif(config_options.grid_type == "unstructured"):
                    # Regrid the height variable.
                    if mpi_config.rank == 0:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    else:
                        var_tmp = None
                    err_handler.check_program_status(config_options, mpi_config)

                    var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                    err_handler.check_program_status(config_options, mpi_config)

                    try:
                        input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to place NetCDF elevation data into the ESMF field object: " \
                                                + str(err)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    if mpi_config.rank == 0:
                        config_options.statusMsg = "Regridding elevation data to the WRF-Hydro domain."
                        err_handler.log_msg(config_options, mpi_config)
                    try:
                        input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                    except ValueError as ve:
                        config_options.errMsg = "Unable to regrid elevation data to the WRF-Hydro domain " \
                                                "using ESMF: " + str(ve)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    # Set any pixel cells outside the input domain to the global missing value.
                    try:
                        input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                            config_options.globalNdv
                    except (ValueError, ArithmeticError) as npe:
                        config_options.errMsg = "Unable to compute mask on elevation data: " + str(npe)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    try:
                        input_forcings.height[:] = input_forcings.esmf_field_out.data
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract ESMF regridded elevation data to a local " \
                                                "array: " + str(err)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    # Regrid the height variable.
                    if mpi_config.rank == 0:
                        var_tmp_elem = id_tmp.variables['HGT_surface'][0, :, :]
                    else:
                        var_tmp_elem = None
                    err_handler.check_program_status(config_options, mpi_config)

                    var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
                    err_handler.check_program_status(config_options, mpi_config)

                    try:
                        input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to place NetCDF elevation data into the ESMF field object: " \
                                                + str(err)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    if mpi_config.rank == 0:
                        config_options.statusMsg = "Regridding elevation data to the WRF-Hydro domain."
                        err_handler.log_msg(config_options, mpi_config)
                    try:
                        input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                             input_forcings.esmf_field_out_elem)
                    except ValueError as ve:
                        config_options.errMsg = "Unable to regrid elevation data to the WRF-Hydro domain " \
                                                "using ESMF: " + str(ve)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    # Set any pixel cells outside the input domain to the global missing value.
                    try:
                        input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                            config_options.globalNdv
                    except (ValueError, ArithmeticError) as npe:
                        config_options.errMsg = "Unable to compute mask on elevation data: " + str(npe)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    try:
                        input_forcings.height_elem[:] = input_forcings.esmf_field_out_elem.data
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract ESMF regridded elevation data to a local " \
                                                "array: " + str(err)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                elif(config_options.grid_type == "hydrofabric"):
                    # Regrid the height variable.
                    if mpi_config.rank == 0:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    else:
                        var_tmp = None
                    err_handler.check_program_status(config_options, mpi_config)

                    var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                    err_handler.check_program_status(config_options, mpi_config)

                    try:
                        input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to place NetCDF elevation data into the ESMF field object: " \
                                                + str(err)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    if mpi_config.rank == 0:
                        config_options.statusMsg = "Regridding elevation data to the WRF-Hydro domain."
                        err_handler.log_msg(config_options, mpi_config)
                    try:
                        input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                    except ValueError as ve:
                        config_options.errMsg = "Unable to regrid elevation data to the WRF-Hydro domain " \
                                                "using ESMF: " + str(ve)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    # Set any pixel cells outside the input domain to the global missing value.
                    try:
                        input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                            config_options.globalNdv
                    except (ValueError, ArithmeticError) as npe:
                        config_options.errMsg = "Unable to compute mask on elevation data: " + str(npe)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    try:
                        input_forcings.height[:] = input_forcings.esmf_field_out.data
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract ESMF regridded elevation data to a local " \
                                                "array: " + str(err)
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

            else:
                input_forcings.height = None
                if mpi_config.rank == 0:
                    config_options.statusMsg = f"Unable to locate HGT_surface in: {input_forcings.file_in2}. " \
                                               f"Downscaling will not be available."
                    err_handler.log_msg(config_options, mpi_config)

            # close netCDF file on non-root ranks
            if mpi_config.rank != 0:
                id_tmp.close()

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            fill = fill_values.get(input_forcings.grib_vars[force_count], config_options.globalNdv)
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using {fill} to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp = id_tmp.variables[nc_var][:].filled(fill)[0, :, :]
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = fill
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input Custom netCDF regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'APCP_surface':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != fill)
                            # Flag to set regridded mask for AORC to overlay with ERA5-Interim blend
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[
                                                                        ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on Custom netCDF precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            fill = fill_values.get(input_forcings.grib_vars[force_count], config_options.globalNdv)
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using {fill} to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp = id_tmp.variables[nc_var][:].filled(fill)[0, :, :]
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = fill
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input Custom netCDF regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'APCP_surface':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != fill)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[
                                                                        ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on Custom netCDF precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

            # Regrid the input variables.
            var_tmp_elem = None
            fill = fill_values.get(input_forcings.grib_vars[force_count], config_options.globalNdv)
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using {fill} to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp_elem = id_tmp.variables[nc_var][:].filled(fill)[0, :, :]
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = fill
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input Custom netCDF regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'APCP_surface':
                try:
                    ind_valid_elem = np.where(input_forcings.esmf_field_out_elem.data != fill)
                    input_forcings.esmf_field_out_elem.data[ind_valid_elem] = input_forcings.esmf_field_out_elem.data[
                                                                        ind_valid_elem] / 3600.0
                    del ind_valid_elem
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on Custom netCDF precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out_elem.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            fill = fill_values.get(input_forcings.grib_vars[force_count], config_options.globalNdv)
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using {fill} to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp = id_tmp.variables[nc_var][:].filled(fill)[0, :, :]
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)
            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = fill
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input Custom netCDF regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if nc_var == 'DSWRF_surface':
                ind_valid = np.where(input_forcings.esmf_field_out.data < 0.0)
                input_forcings.esmf_field_out.data[ind_valid] = 0.0
            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'APCP_surface':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != fill)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[
                                                                        ind_valid] / 3600.0
                    ind_valid = np.where(input_forcings.esmf_field_out.data < 0.0)
                    input_forcings.esmf_field_out.data[ind_valid] = 0.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on Custom netCDF precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)
    # Close the NetCDF file
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.err_out(config_options)


def regrid_era5(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of a single ERA5 netcdf custom file.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in1):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No ERA5-Interim regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return

    # Open the input NetCDF file containing necessary data.
    id_tmp = ioMod.open_netcdf_forcing(input_forcings.file_in2, config_options, mpi_config, open_on_all_procs=True)

    # create netcdf time stamp
    time = nc.num2date(id_tmp.variables['time'][:].data,units=id_tmp.variables['time'].units,only_use_cftime_datetimes=False)
    # Find the timestamp index based on latest timestamp requested
    # by the forcings engine
    seconds_index = np.abs((pd.to_datetime(time) - pd.to_datetime(config_options.current_time)).total_seconds())
    ind = np.where(seconds_index == np.min(seconds_index))[0][0]

    for force_count, nc_var in enumerate(input_forcings.netcdf_var_names):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing ERA-5 Interim Forcing Variable: " + nc_var
            err_handler.log_msg(config_options, mpi_config)
        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)
        if calc_regrid_flag:
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)

            # Read in the ERA5-Interim height field, which is used for downscaling purposes.
            if 'Geopotential' in id_tmp.variables.keys():
                print('Found geopotential height in ERA5-Interim data')
            # To Do, see if NCAR downscaling methods are applicable
            # for reanalysis datasets with coarser resolution
            else:
                input_forcings.height = None
                if mpi_config.rank == 0:
                    config_options.statusMsg = f"Unable to locate Geopoential height in: {input_forcings.file_in2}. " \
                                               f"Downscaling will not be available."
                    err_handler.log_msg(config_options, mpi_config)

            # close netCDF file on non-root ranks
            if mpi_config.rank != 0:
                id_tmp.close()

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding ERA5-Interim input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using -9999. to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp = id_tmp.variables[nc_var][:].filled(-9999.)[ind, :, :]
                    # Since ERA5-Interim only supplies dew points
                    # we will need to calculate the specific humidity
                    if(nc_var == "d2m"):
                        var_tmp = var_tmp -273.15
                        e = 6.112*np.exp((17.67*var_tmp)/(var_tmp+243.5))
                        pres = id_tmp.variables['sp'][:].filled(-9999.)[ind, :, :]/100
                        var_tmp = (0.622*e)/(pres-(0.378*e))
                        del e
                        del pres
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input ERA5-Interim forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = -9999.
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input ERA5-Interim regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'mtpr':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != -9999.)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[
                                                                        ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on ERA5-Interim precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding ERA5-Interim input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using -9999. to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp = id_tmp.variables[nc_var][:].filled(-9999.)[ind, :, :]
                    # Since ERA5-Interim only supplies dew points
                    # we will need to calculate the specific humidity
                    if(nc_var == "d2m"):
                        var_tmp = var_tmp -273.15
                        e = 6.112*np.exp((17.67*var_tmp)/(var_tmp+243.5))
                        pres = id_tmp.variables['sp'][:].filled(-9999.)[ind, :, :]/100
                        var_tmp = (0.622*e)/(pres-(0.378*e))
                        del e
                        del pres
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input ERA5-Interim forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = -9999.
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input ERA5-Interim regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'mtpr':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != -9999.)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[
                                                                        ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on ERA5-Interim precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count],:] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

            # Regrid the input variables.
            var_tmp_elem = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding ERA5-Interim input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using -9999. to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp_elem = id_tmp.variables[nc_var][:].filled(-9999.)[ind, :, :]
                    # Since ERA5-Interim only supplies dew points
                    # we will need to calculate the specific humidity
                    if(nc_var == "d2m"):
                        var_tmp_elem = var_tmp_elem -273.15
                        e = 6.112*np.exp((17.67*var_tmp_elem)/(var_tmp_elem+243.5))
                        pres = id_tmp.variables['sp'][:].filled(-9999.)[ind, :, :]/100
                        var_tmp = (0.622*e)/(pres-(0.378*e))
                        del e
                        del pres
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input ERA5-Interim forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = -9999.
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input Custom netCDF regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'mtpr':
                try:
                    ind_valid_elem = np.where(input_forcings.esmf_field_out_elem.data != -9999.)
                    input_forcings.esmf_field_out_elem.data[ind_valid_elem] = input_forcings.esmf_field_out_elem.data[
                                                                        ind_valid_elem] / 3600.0
                    del ind_valid_elem
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on ERA5-Interim precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count],:] = \
                    input_forcings.esmf_field_out_elem.data

            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding ERA5-Interim input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using -9999. to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp = id_tmp.variables[nc_var][:].filled(-9999.)[ind, :, :]
                    # Since ERA5-Interim only supplies dew points
                    # we will need to calculate the specific humidity
                    if(nc_var == "d2m"):
                        var_tmp = var_tmp -273.15
                        e = 6.112*np.exp((17.67*var_tmp)/(var_tmp+243.5))
                        pres = id_tmp.variables['sp'][:].filled(-9999.)[ind, :, :]/100
                        var_tmp = (0.622*e)/(pres-(0.378*e))
                        del e
                        del pres
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input ERA5-Interim forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = -9999.
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input ERA5-Interim regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'mtpr':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != -9999.)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[
                                                                        ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on ERA5-Interim precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count],:] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

    # Close the NetCDF file
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.err_out(config_options)


@static_vars(last_file=None)
def regrid_gfs(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handing regridding of input GFS data
    fro GRIB2 files.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in2):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No 13km GFS regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return

    # Create a path for a temporary NetCDF file
    input_forcings.tmpFile = config_options.scratch_dir + "/" + "GFS_TMP.nc"
    err_handler.check_program_status(config_options, mpi_config)

    # check / set previous file to see if we're going to reuse
    reuse_prev_file = (input_forcings.file_in2 == regrid_gfs.last_file)
    regrid_gfs.last_file = input_forcings.file_in2

    # This file may exist. If it does, and we don't need it again, remove it.....
    if not reuse_prev_file and mpi_config.rank == 0:
        if os.path.isfile(input_forcings.tmpFile):
            config_options.statusMsg = "Found old temporary file: " + \
                                       input_forcings.tmpFile + " - Removing....."
            err_handler.log_warning(config_options, mpi_config)
            try:
                os.remove(input_forcings.tmpFile)
            except OSError:
                config_options.errMsg = "Unable to remove file: " + input_forcings.tmpFile
                err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    # We will process each variable at a time. Unfortunately, wgrib2 makes it a bit
    # difficult to handle forecast strings, otherwise this could be done in one command.
    # This makes a compelling case for the use of a GRIB Python API in the future....
    # Incoming shortwave radiation flux.....

    # Loop through all of the input forcings in GFS data. Convert the GRIB2 files
    # to NetCDF, read in the data, regrid it, then map it to the appropriate
    # array slice in the output arrays.

    if reuse_prev_file:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Reusing previous input file: " + input_forcings.file_in2
            err_handler.log_msg(config_options, mpi_config)
        id_tmp = ioMod.open_netcdf_forcing(input_forcings.tmpFile, config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        if input_forcings.fileType != NETCDF:
            fields = []
            for force_count, grib_var in enumerate(input_forcings.grib_vars):
                if mpi_config.rank == 0:
                    config_options.statusMsg = "Converting 13km GFS Variable: " + grib_var
                    err_handler.log_msg(config_options, mpi_config)
                # Create a temporary NetCDF file from the GRIB2 file.
                if grib_var == "PRATE":
                    # By far the most complicated of output variables. We need to calculate
                    # our 'average' PRATE based on our current hour.
                    if input_forcings.fcst_hour2 <= 384:
                        tmp_hr_current = input_forcings.fcst_hour2

                        diff_tmp = tmp_hr_current % 6 if tmp_hr_current % 6 > 0 else 6
                        tmp_hr_previous = tmp_hr_current - diff_tmp

                    else:
                        tmp_hr_previous = input_forcings.fcst_hour1

                    fields.append(':' + grib_var + ':' +
                                  input_forcings.grib_levels[force_count] + ':' +
                                  str(tmp_hr_previous) + '-' + str(input_forcings.fcst_hour2) + " hour ave fcst:")
                else:
                    fields.append(':' + grib_var + ':' +
                                  input_forcings.grib_levels[force_count] + ':'
                                  + str(input_forcings.fcst_hour2) + " hour fcst:")

            # if calc_regrid_flag:
            fields.append(":(HGT):(surface):")
            if(WGRIB2_env):
                cmd = '$WGRIB2 -match "(' + '|'.join(fields) + ')" ' + input_forcings.file_in2 + \
                      " -netcdf " + input_forcings.tmpFile
            else:
                cmd = '(' + '|'.join(fields) + ')'

            id_tmp = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFile, cmd,
                                      config_options, mpi_config, inputVar=None, special_case=False)
            err_handler.check_program_status(config_options, mpi_config)
        else:
            create_link("GFS", input_forcings.file_in2, input_forcings.tmpFile, config_options, mpi_config)
            id_tmp = ioMod.open_netcdf_forcing(input_forcings.tmpFile, config_options, mpi_config)

    for force_count, grib_var in enumerate(input_forcings.grib_vars):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing 13km GFS Variable: " + grib_var
            err_handler.log_msg(config_options, mpi_config)

        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if calc_regrid_flag:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Calculating 13km GFS regridding weights."
                err_handler.log_msg(config_options, mpi_config)
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)
            err_handler.check_program_status(config_options, mpi_config)

            # Read in the GFS height field, which is used for downscaling purposes.
            # if mpi_config.rank == 0:
            #    config_options.statusMsg = "Reading in 13km GFS elevation data."
            #    err_handler.log_msg(config_options, mpi_config)
            # cmd = "$WGRIB2 " + input_forcings.file_in2 + " -match " + \
            #    "\":(HGT):(surface):\" " + \
            #    " -netcdf " + input_forcings.tmpFileHeight
            # time.sleep(1)
            # id_tmp_height = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFileHeight,
            #                                 cmd, config_options, mpi_config, 'HGT_surface')
            # err_handler.check_program_status(config_options, mpi_config)

            # Regrid the height variable.
            if(config_options.grid_type == "gridded"):
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract GFS elevation from: " + input_forcings.tmpFile + \
                                            " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)
            elif(config_options.grid_type == "unstructured"):
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract GFS elevation from: " + input_forcings.tmpFile + \
                                            " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                var_tmp_elem = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp_elem = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract GFS elevation from: " + input_forcings.tmpFile + \
                                            " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
                err_handler.check_program_status(config_options, mpi_config)
            elif(config_options.grid_type == "hydrofabric"):
                var_tmp = None
                if mpi_config.rank == 0:
                    try:
                        var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                    except (ValueError, KeyError, AttributeError) as err:
                        config_options.errMsg = "Unable to extract GFS elevation from: " + input_forcings.tmpFile + \
                                            " (" + str(err) + ")"
                        err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local GFS array into an ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding 13km GFS surface elevation data to the WRF-Hydro domain."
                err_handler.log_msg(config_options, mpi_config)
            if(config_options.grid_type == "gridded"):
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid GFS elevation data: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform mask search on GFS elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "unstructured"):
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid GFS elevation data with ESMF mesh nodes: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place local GFS array into an ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                                 input_forcings.esmf_field_out_elem)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid GFS elevation data with ESMF mesh elements: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform mask search on GFS elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform mask search on GFS elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)
            elif(config_options.grid_type == "hydrofabric"):
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid GFS elevation data: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to perform mask search on GFS elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)


            if(config_options.grid_type == "gridded"):
                try:
                    input_forcings.height[:, :] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract GFS elevation array from ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)
            elif(config_options.grid_type == "unstructured"):
                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract GFS elevation array from ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height_elem[:] = input_forcings.esmf_field_out_elem.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract GFS elevation array from ESMF field with mesh elements: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "hydrofabric"):
                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract GFS elevation array from ESMF field: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # Close the temporary NetCDF file and remove it.
            # if mpi_config.rank == 0:
            #    try:
            #        id_tmp_height.close()
            #    except OSError:
            #        config_options.errMsg = "Unable to close temporary file: " + input_forcings.tmpFileHeight
            #        err_handler.log_critical(config_options, mpi_config)

            #    try:
            #        os.remove(input_forcings.tmpFileHeight)
            #    except OSError:
            #        config_options.errMsg = "Unable to remove temporary file: " + input_forcings.tmpFileHeight
            #        err_handler.log_critical(config_options, mpi_config)
            # err_handler.check_program_status(config_options, mpi_config)

        # Regrid the input variables.
        if(config_options.grid_type == "gridded"):
            var_tmp = None
            if mpi_config.rank == 0:
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
        elif(config_options.grid_type == "unstructured"):
            var_tmp = None
            if mpi_config.rank == 0:
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_tmp_elem = None
            if mpi_config.rank == 0:
                try:
                    var_tmp_elem = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
        elif(config_options.grid_type == "hydrofabric"):
            var_tmp = None
            if mpi_config.rank == 0:
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        # If we are regridding GFS data, and this is precipitation, we need to run calculations
        # on the global precipitation average rates to calculate instantaneous global rates.
        # This is due to GFS's weird nature of doing average rates over different periods.
        if input_forcings.productName == "GFS_Production_GRIB2":
            if grib_var == "PRATE":
                if mpi_config.rank == 0:
                    if(config_options.grid_type == "gridded"):
                        input_forcings.globalPcpRate2 = var_tmp
                        var_tmp = timeInterpMod.gfs_pcp_time_interp(input_forcings, config_options, mpi_config)
                    elif(config_options.grid_type == "unstructured"):
                        input_forcings.globalPcpRate2 = var_tmp
                        input_forcings.globalPcpRate2_elem = var_tmp_elem
                        var_tmp, var_tmp_elem = timeInterpMod.gfs_pcp_time_interp(input_forcings, config_options, mpi_config)
                    elif(config_options.grid_type == "hydrofabric"):
                        input_forcings.globalPcpRate2 = var_tmp
                        var_tmp = timeInterpMod.gfs_pcp_time_interp(input_forcings, config_options, mpi_config)

        if grib_var == 'CPOFP':
            if mpi_config.rank == 0:
                # print(f"DEBUG: CPOFP stats, min={var_tmp[var_tmp > 0].min()} mean={var_tmp[var_tmp > 0].mean()} max={var_tmp[var_tmp > 0].max()}", flush=True)
                var_tmp[var_tmp >=0] = (100 - var_tmp[var_tmp >=0]) / 100  # convert frozen fraction to liquid fraction
                var_tmp[var_tmp < 0] = 1.0                         # assume all liquid if not specifically given
                if(config_options.grid_type == "unstructured"):
                    var_tmp_elem[var_tmp_elem >=0] = (100 - var_tmp_elem[var_tmp_elem >=0]) / 100  # convert frozen fraction to liquid fraction
                    var_tmp_elem[var_tmp_elem < 0] = 1.0                         # assume all liquid if not specifically given

        if(config_options.grid_type == "gridded"):
            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            mpi_config.comm.barrier()
            err_handler.check_program_status(config_options, mpi_config)
        elif(config_options.grid_type == "unstructured"):
            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            mpi_config.comm.barrier()
            err_handler.check_program_status(config_options, mpi_config)
            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            mpi_config.comm.barrier()
            err_handler.check_program_status(config_options, mpi_config)
        elif(config_options.grid_type == "hydrofabric"):
            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            mpi_config.comm.barrier()
            err_handler.check_program_status(config_options, mpi_config)


        if(config_options.grid_type == "gridded"):
            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place GFS local array into ESMF element field object: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
        if(config_options.grid_type == "unstructured"):
            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place GFS local array into ESMF element field object: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place GFS local array into ESMF element field object: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
        elif(config_options.grid_type == "hydrofabric"):
            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place GFS local array into ESMF element field object: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input 13km GFS Field: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                begin = time.monotonic()
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
                end = time.monotonic()
                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding took {} seconds".format(end-begin)
                    err_handler.log_msg(config_options, mpi_config)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid GFS variable: " + input_forcings.netcdf_var_names[force_count] \
                                        + " (" + str(ve) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask search on GFS variable: " + \
                                        input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract GFS ESMF field data to local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input 13km GFS Field for Mesh nodes: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                begin = time.monotonic()
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
                end = time.monotonic()
                if mpi_config.rank == 0:
                    config_options.statusMsg = "Node Regridding took {} seconds".format(end-begin)
                    err_handler.log_msg(config_options, mpi_config)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid GFS variable for Mesh Nodes: " + input_forcings.netcdf_var_names[force_count] \
                                        + " (" + str(ve) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask search on GFS variable: " + \
                                        input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract GFS ESMF field data to local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input 13km GFS Field for Mesh elements: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                begin = time.monotonic()
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
                end = time.monotonic()
                if mpi_config.rank == 0:
                    config_options.statusMsg = "Element Regridding took {} seconds".format(end-begin)
                    err_handler.log_msg(config_options, mpi_config)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid GFS variable for Mesh Elements: " + input_forcings.netcdf_var_names[force_count] \
                                        + " (" + str(ve) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask search on GFS variable: " + \
                                        input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out_elem.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract GFS ESMF field data to local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "hydrofabric"):
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Input 13km GFS Field for Mesh Elements: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
            try:
                begin = time.monotonic()
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
                end = time.monotonic()
                if mpi_config.rank == 0:
                    config_options.statusMsg = "Element Regridding took {} seconds".format(end-begin)
                    err_handler.log_msg(config_options, mpi_config)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid GFS variable for Mesh Element: " + input_forcings.netcdf_var_names[force_count] \
                                        + " (" + str(ve) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask search on GFS variable: " + \
                                        input_forcings.netcdf_var_names[force_count] + " (" + str(npe) + ")"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract GFS ESMF field data to local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)

        # DON'T REMOVE THE FILE, IT WILL EITHER BE REUSED or OVERWRITTEN
        # try:
        #     os.remove(input_forcings.tmpFile)
        # except OSError:
        #     config_options.errMsg = "Unable to remove NetCDF file: " + input_forcings.tmpFile
        #     err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)


def regrid_nam_nest(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handing regridding of input NAM nest data
    fro GRIB2 files.
    :param mpi_config:
    :param wrf_hydro_geo_meta:
    :param input_forcings:
    :param config_options:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in2):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        config_options.statusMsg = "No regridding of NAM nest data necessary for this timestep - already completed."
        err_handler.log_msg(config_options, mpi_config)
        return

    # Create a path for a temporary NetCDF file
    input_forcings.tmpFile = config_options.scratch_dir + "/" + "NAM_NEST_TMP-{}.nc".format(mkfilename())
    err_handler.check_program_status(config_options, mpi_config)
    if input_forcings.fileType != NETCDF:

        # This file shouldn't exist.... but if it does (previously failed
        # execution of the program), remove it.....
        if mpi_config.rank == 0:
            if os.path.isfile(input_forcings.tmpFile):
                config_options.statusMsg = "Found old temporary file: " + \
                                           input_forcings.tmpFile + " - Removing....."
                err_handler.log_warning(config_options, mpi_config)
                try:
                    os.remove(input_forcings.tmpFile)
                except OSError:
                    err_handler.err_out(config_options)
        err_handler.check_program_status(config_options, mpi_config)

        fields = []
        for force_count, grib_var in enumerate(input_forcings.grib_vars):
            if mpi_config.rank == 0:
                config_options.statusMsg = "Converting NAM-Nest Variable: " + grib_var
                err_handler.log_msg(config_options, mpi_config)
            fields.append(':' + grib_var + ':' +
                          input_forcings.grib_levels[force_count] + ':'
                          + str(input_forcings.fcst_hour2) + " hour fcst:")
        fields.append(":(HGT):(surface):")

        # Create a temporary NetCDF file from the GRIB2 file.
        if(WGRIB2_env):
            cmd = '$WGRIB2 -match "(' + '|'.join(fields) + ')" ' + input_forcings.file_in2 + \
                  " -netcdf " + input_forcings.tmpFile
        else:
            cmd = '(' + '|'.join(fields) + ')'

        id_tmp = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFile, cmd,
                                  config_options, mpi_config, inputVar=None, special_case=False)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        create_link("NAM-Nest", input_forcings.file_in2, input_forcings.tmpFile, config_options, mpi_config)
        id_tmp = ioMod.open_netcdf_forcing(input_forcings.tmpFile, config_options, mpi_config)

    # Loop through all of the input forcings in NAM nest data. Convert the GRIB2 files
    # to NetCDF, read in the data, regrid it, then map it to the appropriate
    # array slice in the output arrays.
    for force_count, grib_var in enumerate(input_forcings.grib_vars):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing NAM Nest Variable: " + grib_var
            err_handler.log_msg(config_options, mpi_config)

        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if calc_regrid_flag:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Calculating NAM nest regridding weights...."
                err_handler.log_msg(config_options, mpi_config)
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)
            err_handler.check_program_status(config_options, mpi_config)

            # Read in the RAP height field, which is used for downscaling purposes.
            # if mpi_config.rank == 0:
            #     config_options.statusMsg = "Reading in NAM nest elevation data from GRIB2."
            #     err_handler.log_msg(config_options, mpi_config)
            # cmd = "$WGRIB2 " + input_forcings.file_in2 + " -match " + \
            #       "\":(HGT):(surface):\" " + \
            #       " -netcdf " + input_forcings.tmpFileHeight
            # id_tmp_height = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFileHeight,
            #                                  cmd, config_options, mpi_config, 'HGT_surface')
            # err_handler.check_program_status(config_options, mpi_config)
            if(config_options.grid_type == "gridded"):
                # Regrid the height variable.
                if mpi_config.rank == 0:
                    var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                else:
                    var_tmp = None
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place NetCDF NAM nest elevation data into the ESMF field object: " \
                                            + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding NAM nest elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid NAM nest elevation data to the WRF-Hydro domain " \
                                            "using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to compute mask on NAM nest elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:, :] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF regridded NAM nest elevation data to a local " \
                                            "array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "unstructured"):
                # Regrid the height variable.
                if mpi_config.rank == 0:
                    var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                else:
                    var_tmp = None
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place NetCDF NAM nest elevation data into the ESMF field object: " \
                                            + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding NAM nest elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid NAM nest elevation data to the WRF-Hydro domain " \
                                            "using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to compute mask on NAM nest elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF regridded NAM nest elevation data to a local " \
                                            "array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Regrid the height variable.
                if mpi_config.rank == 0:
                    var_tmp_elem = id_tmp.variables['HGT_surface'][0, :, :]
                else:
                    var_tmp_elem = None
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place NetCDF NAM nest elevation data into the ESMF field object: " \
                                            + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding NAM nest elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid NAM nest elevation data to the WRF-Hydro domain " \
                                            "using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to compute mask on NAM nest elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height_elem[:] = input_forcings.esmf_field_out_elem.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF regridded NAM nest elevation data to a local " \
                                            "array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "hydrofabric"):
                # Regrid the height variable.
                if mpi_config.rank == 0:
                    var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                else:
                    var_tmp = None
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place NetCDF NAM nest elevation data into the ESMF field object: " \
                                            + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding NAM nest elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid NAM nest elevation data to the WRF-Hydro domain " \
                                            "using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to compute mask on NAM nest elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF regridded NAM nest elevation data to a local " \
                                            "array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # Close the temporary NetCDF file and remove it.
            # if mpi_config.rank == 0:
            #     try:
            #         id_tmp_height.close()
            #     except OSError:
            #         config_options.errMsg = "Unable to close temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
            #
            #     try:
            #         os.remove(input_forcings.tmpFileHeight)
            #     except OSError:
            #         config_options.errMsg = "Unable to remove temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
            # err_handler.check_program_status(config_options, mpi_config)

        err_handler.check_program_status(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding NAM nest input variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input NAM nest forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input NAM nest regridded forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding NAM nest input variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input NAM nest forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input NAM nest regridded forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

            # Regrid the input variables.
            var_tmp_elem = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding NAM nest input variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp_elem = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input NAM nest forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input NAM nest regridded forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out_elem.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding NAM nest input variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input NAM nest forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input NAM nest regridded forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
        try:
            os.remove(input_forcings.tmpFile)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)


def regrid_mrms_hourly(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding hourly MRMS precipitation. An RQI mask file
    Is necessary to filter out poor precipitation estimates.
    :param supplemental_precip:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """

    # Do we want to use MRMS data at this timestep? If not, log and continue
    if not config_options.use_data_at_current_time:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Exceeded max hours for MRMS precipitation"
            err_handler.log_msg(config_options, mpi_config)
        return

    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(supplemental_precip.file_in2):
        # does file_in1 exist? (Pass1 vs Pass2 for MRMS, for example)
        if os.path.isfile(supplemental_precip.file_in1):
            supplemental_precip.file_in2 = supplemental_precip.file_in1
        else:
            return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if supplemental_precip.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No MRMS regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return
    # MRMS data originally is stored as .gz files. We need to compose a series
    # of temporary paths.
    # 1.) The unzipped GRIB2 precipitation file.
    # 2.) The unzipped GRIB2 RQI file.
    # 3.) A temporary NetCDF file that stores the precipitation grid.
    # 4.) A temporary NetCDF file that stores the RQI grid.
    # Create a path for a temporary NetCDF files that will
    # be created through the wgrib2 process.
    mrms_tmp_grib2 = config_options.scratch_dir + "/MRMS_PCP_TMP-{}.grib2".format(mkfilename())
    mrms_tmp_nc = config_options.scratch_dir + "/MRMS_PCP_TMP-{}.nc".format(mkfilename())
    mrms_tmp_rqi_grib2 = config_options.scratch_dir + "/MRMS_RQI_TMP-{}.grib2".format(mkfilename())
    mrms_tmp_rqi_nc = config_options.scratch_dir + "/MRMS_RQI_TMP-{}.nc".format(mkfilename())
    # mpi_config.comm.barrier()

    # If the input paths have been set to None, this means input is missing. We will
    # alert the user, and set the final output grids to be the global NDV and return.
    if not supplemental_precip.file_in1 or not supplemental_precip.file_in2:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No MRMS Precipitation available. Supplemental precipitation will " \
                                       "not be used."
            err_handler.log_msg(config_options, mpi_config)
        supplemental_precip.regridded_precip2 = None
        supplemental_precip.regridded_precip1 = None
        if(config_options.grid_type == "unstructured"):
            supplemental_precip.regridded_precip2_elem = None
            supplemental_precip.regridded_precip1_elem = None
        return

    # These files shouldn't exist. If they do, remove them.
    if mpi_config.rank == 0:
        if os.path.isfile(mrms_tmp_grib2):
            config_options.statusMsg = "Found old temporary file: " + \
                                       mrms_tmp_grib2 + " - Removing....."
            err_handler.log_warning(config_options, mpi_config)
            try:
                os.remove(mrms_tmp_grib2)
            except OSError:
                config_options.errMsg = "Unable to remove file: " + mrms_tmp_grib2
                err_handler.log_critical(config_options, mpi_config)
        if os.path.isfile(mrms_tmp_nc):
            config_options.statusMsg = "Found old temporary file: " + \
                                       mrms_tmp_nc + " - Removing....."
            err_handler.log_warning(config_options, mpi_config)
            try:
                os.remove(mrms_tmp_nc)
            except OSError:
                config_options.errMsg = "Unable to remove file: " + mrms_tmp_nc
                err_handler.log_critical(config_options, mpi_config)
        if os.path.isfile(mrms_tmp_rqi_grib2):
            config_options.statusMsg = "Found old temporary file: " + \
                                       mrms_tmp_rqi_grib2 + " - Removing....."
            err_handler.log_warning(config_options, mpi_config)
            try:
                os.remove(mrms_tmp_rqi_grib2)
            except OSError:
                config_options.errMsg = "Unable to remove file: " + mrms_tmp_rqi_grib2
                err_handler.log_critical(config_options, mpi_config)
        if os.path.isfile(mrms_tmp_rqi_nc):
            config_options.statusMsg = "Found old temporary file: " + \
                                       mrms_tmp_rqi_nc + " - Removing....."
            err_handler.log_warning(config_options, mpi_config)
            try:
                os.remove(mrms_tmp_rqi_nc)
            except OSError:
                config_options.errMsg = "Unable to remove file: " + mrms_tmp_rqi_nc
                err_handler.log_critical(config_options, mpi_config)

    err_handler.check_program_status(config_options, mpi_config)

    # If the input paths have been set to None, this means input is missing. We will
    # alert the user, and set the final output grids to be the global NDV and return.
    # if not supplemental_precip.file_in1 or not supplemental_precip.file_in2:
    #    if MpiConfig.rank == 0:
    #        ConfigOptions.statusMsg = "No MRMS Precipitation available. Supplemental precipitation will " \
    #                                  "not be used."
    #        errMod.log_msg(ConfigOptions, MpiConfig)
    #    supplemental_precip.regridded_precip2 = None
    #    supplemental_precip.regridded_precip1 = None
    #    return

    if supplemental_precip.fileType != NETCDF:
        # Unzip MRMS files to temporary locations.
        ioMod.unzip_file(supplemental_precip.file_in2, mrms_tmp_grib2, config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if supplemental_precip.rqiMethod == 1:
            ioMod.unzip_file(supplemental_precip.rqi_file_in2, mrms_tmp_rqi_grib2, config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        # Perform a GRIB dump to NetCDF for the MRMS precip and RQI data.
        if(WGRIB2_env):
            cmd1 = "$WGRIB2 " + mrms_tmp_grib2 + " -netcdf " + mrms_tmp_nc
        else:
            cmd1 = mrms_tmp_grib2

        id_mrms = ioMod.open_grib2(mrms_tmp_grib2, mrms_tmp_nc, cmd1, config_options,
                                   mpi_config, supplemental_precip.netcdf_var_names[0],special_case=True)
        err_handler.check_program_status(config_options, mpi_config)

        if supplemental_precip.rqiMethod == 1:
            if(WGRIB2_env):
                cmd2 = "$WGRIB2 " + mrms_tmp_rqi_grib2 + " -netcdf " + mrms_tmp_rqi_nc
            else:
                cmd2 = mrms_tmp_rqi_grib2

            id_mrms_rqi = ioMod.open_grib2(mrms_tmp_rqi_grib2, mrms_tmp_rqi_nc, cmd2, config_options,
                                           mpi_config, supplemental_precip.rqi_netcdf_var_names[0], special_case=True)
            err_handler.check_program_status(config_options, mpi_config)
        else:
            id_mrms_rqi = None

        # Remove temporary GRIB2 files
        if mpi_config.rank == 0:
            try:
                os.remove(mrms_tmp_grib2)
            except OSError:
                config_options.errMsg = "Unable to remove GRIB2 file: " + mrms_tmp_grib2
                err_handler.log_critical(config_options, mpi_config)

            if supplemental_precip.rqiMethod == 1:
                try:
                    os.remove(mrms_tmp_rqi_grib2)
                except OSError:
                    config_options.errMsg = "Unable to remove GRIB2 file: " + mrms_tmp_rqi_grib2
                    err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        create_link("MRMS", supplemental_precip.file_in2, mrms_tmp_nc, config_options, mpi_config)
        id_mrms = ioMod.open_netcdf_forcing(mrms_tmp_nc, config_options, mpi_config)
        if supplemental_precip.rqiMethod == 1:
            create_link("RQI", supplemental_precip.rqi_file_in2, mrms_tmp_rqi_nc, config_options, mpi_config)
            id_mrms_rqi = ioMod.open_netcdf_forcing(mrms_tmp_rqi_nc, config_options, mpi_config)
        else:
            id_mrms_rqi = None

    # Check to see if we need to calculate regridding weights.
    calc_regrid_flag = check_supp_pcp_regrid_status(id_mrms, supplemental_precip, config_options,
                                                    wrf_hydro_geo_meta, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    if calc_regrid_flag:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Calculating MRMS regridding weights."
            err_handler.log_msg(config_options, mpi_config)
        calculate_supp_pcp_weights(supplemental_precip, id_mrms, mrms_tmp_nc, config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

    # Regrid the RQI grid.
    if supplemental_precip.rqiMethod == 1:
        if(config_options.grid_type != "unstructured"):
            var_tmp = None
            if mpi_config.rank == 0:
                try:
                    var_tmp = id_mrms_rqi.variables[supplemental_precip.rqi_netcdf_var_names[0]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + supplemental_precip.rqi_netcdf_var_names[0] + \
                                            " from: " + mrms_tmp_rqi_grib2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place MRMS data into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding MRMS RQI Field."
                err_handler.log_msg(config_options, mpi_config)
            try:
                supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                                   supplemental_precip.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid MRMS RQI field: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                n_masked = len((supplemental_precip.regridded_mask == 0))
                if n_masked > 0:
                    if mpi_config == 0:
                        config_options.statusMsg = f"{n_masked} masked cells in RQI field, will remove"
                        err_handler.log_msg(config_options, mpi_config)

                supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask calculation for MRMS RQI data: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            var_tmp = None
            if mpi_config.rank == 0:
                try:
                    var_tmp = id_mrms_rqi.variables[supplemental_precip.rqi_netcdf_var_names[0]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + supplemental_precip.rqi_netcdf_var_names[0] + \
                                            " from: " + mrms_tmp_rqi_grib2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place MRMS data into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding MRMS RQI Field."
                err_handler.log_msg(config_options, mpi_config)
            try:
                supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                                   supplemental_precip.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid MRMS RQI field: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                n_masked = len((supplemental_precip.regridded_mask == 0))
                if n_masked > 0:
                    if mpi_config == 0:
                        config_options.statusMsg = f"{n_masked} masked cells in RQI field, will remove"
                        err_handler.log_msg(config_options, mpi_config)

                supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask calculation for MRMS RQI data: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_tmp_elem = None
            if mpi_config.rank == 0:
                try:
                    var_tmp_elem = id_mrms_rqi.variables[supplemental_precip.rqi_netcdf_var_names[0]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract: " + supplemental_precip.rqi_netcdf_var_names[0] + \
                                            " from: " + mrms_tmp_rqi_grib2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(supplemental_precip, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                supplemental_precip.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place MRMS data into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding MRMS RQI Field."
                err_handler.log_msg(config_options, mpi_config)
            try:
                supplemental_precip.esmf_field_out_elem = supplemental_precip.regridObj_elem(supplemental_precip.esmf_field_in_elem,
                                                                                   supplemental_precip.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid MRMS RQI field: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                n_masked = len((supplemental_precip.regridded_mask_elem == 0))
                if n_masked > 0:
                    if mpi_config == 0:
                        config_options.statusMsg = f"{n_masked} masked cells in RQI field, will remove"
                        err_handler.log_msg(config_options, mpi_config)

                supplemental_precip.esmf_field_out_elem.data[np.where(supplemental_precip.regridded_mask_elem == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask calculation for MRMS RQI data: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

    if not supplemental_precip.rqiMethod:
        # We will set the RQI field to 1.0 here so no MRMS data gets masked out.
        if(config_options.grid_type == "gridded"):
            supplemental_precip.regridded_rqi2[:, :] = 1.0
        elif(config_options.grid_type == "unstructured"):
            supplemental_precip.regridded_rqi2[:] = 1.0
            supplemental_precip.regridded_rqi2_elem[:] = 1.0
        elif(config_options.grid_type == "hydrofabric"):
            supplemental_precip.regridded_rqi2[:] = 1.0

        if mpi_config.rank == 0:
            config_options.statusMsg = "MRMS Will not be filtered using RQI values."
            err_handler.log_msg(config_options, mpi_config)

    elif supplemental_precip.rqiMethod == 2:
        # Read in the RQI field from monthly climatological files.
        ioMod.read_rqi_monthly_climo(config_options, mpi_config, supplemental_precip, wrf_hydro_geo_meta)
    elif supplemental_precip.rqiMethod == 1:
        # We are using the MRMS RQI field in realtime
        if(config_options.grid_type == "gridded"):
            supplemental_precip.regridded_rqi2[:, :] = supplemental_precip.esmf_field_out.data
        elif(config_options.grid_type == "unstructured"):
            supplemental_precip.regridded_rqi2[:] = supplemental_precip.esmf_field_out.data
            supplemental_precip.regridded_rqi2_elem[:] = supplemental_precip.esmf_field_out_elem.data
        elif(config_options.grid_type == "hydrofabric"):
            supplemental_precip.regridded_rqi2[:] = supplemental_precip.esmf_field_out.data

    err_handler.check_program_status(config_options, mpi_config)

    if supplemental_precip.rqiMethod == 1:
        # Close the temporary NetCDF file and remove it.
        if mpi_config.rank == 0:
            try:
                id_mrms_rqi.close()
            except OSError:
                config_options.errMsg = "Unable to close NetCDF file: " + mrms_tmp_rqi_nc
                err_handler.log_critical(config_options, mpi_config)
            try:
                os.remove(mrms_tmp_rqi_nc)
            except OSError:
                config_options.errMsg = "Unable to remove NetCDF file: " + mrms_tmp_rqi_nc
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

    if(config_options.grid_type == "gridded"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            config_options.statusMsg = "Regridding: " + supplemental_precip.netcdf_var_names[0]
            err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_mrms.variables[supplemental_precip.netcdf_var_names[0]][0, :, :]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract: " + supplemental_precip.netcdf_var_names[0] + \
                                        " from: " + mrms_tmp_nc + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid MRMS precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value, and set negative precip values to 0
        try:
            if len(np.argwhere(supplemental_precip.esmf_field_out.data < 0)) > 0:
                supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.esmf_field_out.data < 0)] = config_options.globalNdv
                #config_options.statusMsg = "WARNING: Found negative precipitation values in MRMS data, setting to 0"
                #err_handler.log_warning(config_options, mpi_config)

            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = config_options.globalNdv

        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on MRMS supplemental precip: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:, :] = \
            supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        if supplemental_precip.rqiMethod > 0:
            # Check for any RQI values below the threshold specified by the user.
            # Set these values to global NDV.
            try:
                ind_filter = np.where(supplemental_precip.regridded_rqi2 < supplemental_precip.rqiThresh)
                if len(ind_filter) > 0:
                    if mpi_config.rank == 0:
                        config_options.statusMsg = f"Removing {len(ind_filter)} MRMS cells below RQI threshold of {supplemental_precip.rqiThresh}"
                        err_handler.log_msg(config_options, mpi_config)
                supplemental_precip.regridded_precip2[ind_filter] = config_options.globalNdv
                del ind_filter
            except (ValueError, AttributeError, KeyError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run MRMS RQI threshold search: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        if supplemental_precip.keyValue != 14:
            # Convert the hourly precipitation total to a rate of mm/s
            try:
                ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
                supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
                del ind_valid
            except (ValueError, AttributeError, ArithmeticError, KeyError) as npe:
                config_options.errMsg = "Unable to run global NDV search on MRMS regridded precip: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:, :] = \
                supplemental_precip.regridded_precip2[:, :]
            supplemental_precip.regridded_rqi1[:, :] = \
                supplemental_precip.regridded_rqi2[:, :]
    # mpi_config.comm.barrier()

    elif(config_options.grid_type == "unstructured"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            config_options.statusMsg = "Regridding: " + supplemental_precip.netcdf_var_names[0]
            err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_mrms.variables[supplemental_precip.netcdf_var_names[0]][0, :, :]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract: " + supplemental_precip.netcdf_var_names[0] + \
                                        " from: " + mrms_tmp_nc + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid MRMS precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value, and set negative precip values to 0
        try:
            if len(np.argwhere(supplemental_precip.esmf_field_out.data < 0)) > 0:
                supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.esmf_field_out.data < 0)] = config_options.globalNdv
                #config_options.statusMsg = "WARNING: Found negative precipitation values in MRMS data, setting to 0"
                #err_handler.log_warning(config_options, mpi_config)

            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = config_options.globalNdv

        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on MRMS supplemental precip: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:] = \
            supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        if supplemental_precip.rqiMethod > 0:
            # Check for any RQI values below the threshold specified by the user.
            # Set these values to global NDV.
            try:
                ind_filter = np.where(supplemental_precip.regridded_rqi2 < supplemental_precip.rqiThresh)
                if len(ind_filter) > 0:
                    if mpi_config.rank == 0:
                        config_options.statusMsg = f"Removing {len(ind_filter)} MRMS cells below RQI threshold of {supplemental_precip.rqiThresh}"
                        err_handler.log_msg(config_options, mpi_config)
                supplemental_precip.regridded_precip2[ind_filter] = config_options.globalNdv
                del ind_filter
            except (ValueError, AttributeError, KeyError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run MRMS RQI threshold search: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        if supplemental_precip.keyValue != 14:
            # Convert the hourly precipitation total to a rate of mm/s
            try:
                ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
                supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
                del ind_valid
            except (ValueError, AttributeError, ArithmeticError, KeyError) as npe:
                config_options.errMsg = "Unable to run global NDV search on MRMS regridded precip: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:] = \
                supplemental_precip.regridded_precip2[:]
            supplemental_precip.regridded_rqi1[:] = \
                supplemental_precip.regridded_rqi2[:]
    # mpi_config.comm.barrier()

        # Regrid the input variables.
        var_tmp_elem = None
        if mpi_config.rank == 0:
            config_options.statusMsg = "Regridding: " + supplemental_precip.netcdf_var_names[0]
            err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp_elem = id_mrms.variables[supplemental_precip.netcdf_var_names[0]][0, :, :]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract: " + supplemental_precip.netcdf_var_names[0] + \
                                        " from: " + mrms_tmp_nc + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp_elem = mpi_config.scatter_array(supplemental_precip, var_tmp_elem, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out_elem = supplemental_precip.regridObj_elem(supplemental_precip.esmf_field_in_elem,
                                                                               supplemental_precip.esmf_field_out_elem)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid MRMS precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value, and set negative precip values to 0
        try:
            supplemental_precip.esmf_field_out_elem.data[np.where(supplemental_precip.regridded_mask_elem == 0)] = config_options.globalNdv
            
            if len(np.argwhere(supplemental_precip.esmf_field_out_elem.data < 0)) > 0:
                supplemental_precip.esmf_field_out_elem.data[np.where(supplemental_precip.esmf_field_out_elem.data < 0)] = config_options.globalNdv
                #config_options.statusMsg = "WARNING: Found negative precipitation values in MRMS data, setting to 0"
                #err_handler.log_warning(config_options, mpi_config)

        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on MRMS supplemental precip: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2_elem[:] = \
            supplemental_precip.esmf_field_out_elem.data
        err_handler.check_program_status(config_options, mpi_config)

        if supplemental_precip.rqiMethod > 0:
            # Check for any RQI values below the threshold specified by the user.
            # Set these values to global NDV.
            try:
                ind_filter_elem = np.where(supplemental_precip.regridded_rqi2_elem < supplemental_precip.rqiThresh)
                if len(ind_filter_elem) > 0:
                    if mpi_config.rank == 0:
                        config_options.statusMsg = f"Removing {len(ind_filter)} MRMS cells below RQI threshold of {supplemental_precip.rqiThresh}"
                        err_handler.log_msg(config_options, mpi_config)
                supplemental_precip.regridded_precip2_elem[ind_filter_elem] = config_options.globalNdv
                del ind_filter_elem
            except (ValueError, AttributeError, KeyError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run MRMS RQI threshold search: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        # Convert the hourly precipitation total to a rate of mm/s
        try:
            ind_valid_elem = np.where(supplemental_precip.regridded_precip2_elem != config_options.globalNdv)
            supplemental_precip.regridded_precip2_elem[ind_valid_elem] = supplemental_precip.regridded_precip2_elem[ind_valid_elem] / 3600.0
            del ind_valid_elem
        except (ValueError, AttributeError, ArithmeticError, KeyError) as npe:
            config_options.errMsg = "Unable to run global NDV search on MRMS regridded precip: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1_elem[:] = \
                supplemental_precip.regridded_precip2_elem[:]
            supplemental_precip.regridded_rqi1_elem[:] = \
                supplemental_precip.regridded_rqi2_elem[:]
    # mpi_config.comm.barrier()

    elif(config_options.grid_type == "hydrofabric"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            config_options.statusMsg = "Regridding: " + supplemental_precip.netcdf_var_names[0]
            err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_mrms.variables[supplemental_precip.netcdf_var_names[0]][0, :, :]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract: " + supplemental_precip.netcdf_var_names[0] + \
                                        " from: " + mrms_tmp_nc + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid MRMS precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value, and set negative precip values to 0
        try:
            if len(np.argwhere(supplemental_precip.esmf_field_out.data < 0)) > 0:
                supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.esmf_field_out.data < 0)] = config_options.globalNdv
                #config_options.statusMsg = "WARNING: Found negative precipitation values in MRMS data, setting to 0"
                #err_handler.log_warning(config_options, mpi_config)

            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = config_options.globalNdv

        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on MRMS supplemental precip: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:] = \
            supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        if supplemental_precip.rqiMethod > 0:
            # Check for any RQI values below the threshold specified by the user.
            # Set these values to global NDV.
            try:
                ind_filter = np.where(supplemental_precip.regridded_rqi2 < supplemental_precip.rqiThresh)
                if len(ind_filter) > 0:
                    if mpi_config.rank == 0:
                        config_options.statusMsg = f"Removing {len(ind_filter)} MRMS cells below RQI threshold of {supplemental_precip.rqiThresh}"
                        err_handler.log_msg(config_options, mpi_config)
                supplemental_precip.regridded_precip2[ind_filter] = config_options.globalNdv
                del ind_filter
            except (ValueError, AttributeError, KeyError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run MRMS RQI threshold search: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        if supplemental_precip.keyValue != 14:
            # Convert the hourly precipitation total to a rate of mm/s
            try:
                ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
                supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
                del ind_valid
            except (ValueError, AttributeError, ArithmeticError, KeyError) as npe:
                config_options.errMsg = "Unable to run global NDV search on MRMS regridded precip: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:] = \
                supplemental_precip.regridded_precip2[:]
            supplemental_precip.regridded_rqi1[:] = \
                supplemental_precip.regridded_rqi2[:]
    # mpi_config.comm.barrier()

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_mrms.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + mrms_tmp_nc
            err_handler.log_critical(config_options, mpi_config)

        try:
            os.remove(mrms_tmp_nc)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + mrms_tmp_nc
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

def regrid_mrms_precip_flag(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of SBCv2 Liquid Water Precip forcing files.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.exists(supplemental_precip.file_in2):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if supplemental_precip.regridComplete:
        return

    # Unzip MRMS precip flag file to temporary location.
    fileno = mkfilename()
    mrms_tmp_grib2 = config_options.scratch_dir + f"/MRMS_PCP_FLAG_TMP_{fileno}.grib2"
    mrms_tmp_nc    = config_options.scratch_dir + f"/MRMS_PCP_FLAG_TMP_{fileno}.nc"
    ioMod.unzip_file(supplemental_precip.file_in2, mrms_tmp_grib2, config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    # Perform a GRIB dump to NetCDF for the MRMS precip and RQI data.
    cmd1 = "$WGRIB2 " + mrms_tmp_grib2 + " -netcdf " + mrms_tmp_nc
    id_tmp = ioMod.open_grib2(mrms_tmp_grib2, mrms_tmp_nc, cmd1, config_options,
                                mpi_config, supplemental_precip.netcdf_var_names[0])
    err_handler.check_program_status(config_options, mpi_config)
    # Remove temporary GRIB2 files
    if mpi_config.rank == 0:
        try:
            os.remove(mrms_tmp_grib2)
        except OSError:
            config_options.errMsg = "Unable to remove GRIB2 file: " + mrms_tmp_grib2
            err_handler.log_critical(config_options, mpi_config)

    # Check to see if we need to calculate regridding weights.
    calc_regrid_flag = check_supp_pcp_regrid_status(id_tmp, supplemental_precip, config_options,
                                                    wrf_hydro_geo_meta, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    if calc_regrid_flag:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Calculating MRMS PrecipFlag regridding weights."
            err_handler.log_msg(config_options, mpi_config)
        calculate_supp_pcp_weights(supplemental_precip, id_tmp, supplemental_precip.file_in2,
                                   config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

    # Regrid the input variable
    var_tmp = None
    if mpi_config.rank == 0:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Regridding MRMS PrecipFlag Fraction."
            err_handler.log_msg(config_options, mpi_config)
        try:
            var_tmp = id_tmp.variables[supplemental_precip.netcdf_var_names[0]][0,:,:]
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to extract PrecipFlag from file: " + \
                                    supplemental_precip.file_in2 + " (" + str(err) + ")"
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
    err_handler.check_program_status(config_options, mpi_config)

    try:
        var_sub_tmp[var_sub_tmp <= 0] = 1.0         # all liquid if no other category
        var_sub_tmp[var_sub_tmp == 3] = 0.0         # snow
        var_sub_tmp[var_sub_tmp == 7] = 0.0         # hail
        var_sub_tmp[var_sub_tmp >  0] = 1.0         # all other liquid categories

        supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
    except (ValueError, KeyError, AttributeError) as err:
        config_options.errMsg = "Unable to place MRMS PrecipFlag into local ESMF field: " + str(err)
        err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    try:
        supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                           supplemental_precip.esmf_field_out)
    except ValueError as ve:
        config_options.errMsg = "Unable to regrid MRMS PrecipFlag: " + str(ve)
        err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    # Set any missing data or pixel cells outside the input domain to a default of 100%
    try:
        supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = 1.0
        supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.esmf_field_out.data < 0)] = 1.0
    except (ValueError, ArithmeticError) as npe:
        config_options.errMsg = "Unable to run mask search on MRMS PrecipFlag: " + str(npe)
        err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    supplemental_precip.regridded_precip2[:] = supplemental_precip.esmf_field_out.data
    err_handler.check_program_status(config_options, mpi_config)

    # If we are on the first timestep, set the previous regridded field to be
    # the latest as there are no states for time 0.
    if config_options.current_output_step == 1:
        supplemental_precip.regridded_precip1[:] = \
            supplemental_precip.regridded_precip2[:]
    err_handler.check_program_status(config_options, mpi_config)


    if(config_options.grid_type == "unstructured"):

        # Regrid the input variable
        var_tmp_elem = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding MRMS PrecipFlag Fraction."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp_elem = id_tmp.variables[supplemental_precip.netcdf_var_names[0]][0,:,:]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract PrecipFlag from file: " + \
                                        supplemental_precip.file_in2 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp_elem = mpi_config.scatter_array(supplemental_precip, var_tmp_elem, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            var_sub_tmp_elem[var_sub_tmp_elem <= 0] = 1.0         # all liquid if no other category
            var_sub_tmp_elem[var_sub_tmp_elem == 3] = 0.0         # snow
            var_sub_tmp_elem[var_sub_tmp_elem == 7] = 0.0         # hail
            var_sub_tmp_elem[var_sub_tmp_elem >  0] = 1.0         # all other liquid categories

            supplemental_precip.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place MRMS PrecipFlag into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out_elem = supplemental_precip.regridObj_elem(supplemental_precip.esmf_field_in_elem,
                                                                               supplemental_precip.esmf_field_out_elem)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid MRMS PrecipFlag: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any missing data or pixel cells outside the input domain to a default of 100%
        try:
            supplemental_precip.esmf_field_out_elem.data[np.where(supplemental_precip.regridded_mask_elem == 0)] = 1.0
            supplemental_precip.esmf_field_out_elem.data[np.where(supplemental_precip.esmf_field_out_elem.data < 0)] = 1.0
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on MRMS PrecipFlag: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2_elem[:] = supplemental_precip.esmf_field_out_elem.data
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1_elem[:] = \
                supplemental_precip.regridded_precip2_elem[:]
        err_handler.check_program_status(config_options, mpi_config)

    # Close the NetCDF file
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + supplemental_precip.file_in2
            err_handler.log_critical(config_options, mpi_config)
        try:
            os.remove(mrms_tmp_nc)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + mrms_tmp_nc
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

def regrid_hourly_wrf_arw(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
       Function for handing regridding of input NAM nest data
       fro GRIB2 files.
       :param mpi_config:
       :param wrf_hydro_geo_meta:
       :param input_forcings:
       :param config_options:
       :return:
       """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.isfile(input_forcings.file_in2):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        config_options.statusMsg = "No regridding of WRF-ARW nest data necessary for this timestep - already completed."
        err_handler.log_msg(config_options, mpi_config)
        return

    # Create a path for a temporary NetCDF file
    input_forcings.tmpFile = config_options.scratch_dir + "/" + "ARW_TMP-{}.nc".format(mkfilename())
    err_handler.check_program_status(config_options, mpi_config)

    if input_forcings.fileType != NETCDF:
        # This file shouldn't exist.... but if it does (previously failed
        # execution of the program), remove it.....
        if mpi_config.rank == 0:
            if os.path.isfile(input_forcings.tmpFile):
                config_options.statusMsg = "Found old temporary file: " + \
                                           input_forcings.tmpFile + " - Removing....."
                err_handler.log_warning(config_options, mpi_config)
                try:
                    os.remove(input_forcings.tmpFile)
                except OSError:
                    err_handler.err_out(config_options)
        err_handler.check_program_status(config_options, mpi_config)

        fields = []
        for force_count, grib_var in enumerate(input_forcings.grib_vars):
            if mpi_config.rank == 0:
                config_options.statusMsg = "Converting WRF-ARW Variable: " + grib_var
                err_handler.log_msg(config_options, mpi_config)
            time_str = "{}-{} hour acc fcst".format(input_forcings.fcst_hour1, input_forcings.fcst_hour2) \
                if grib_var == 'APCP' else str(input_forcings.fcst_hour2) + " hour fcst"
            fields.append(':' + grib_var + ':' +
                          input_forcings.grib_levels[force_count] + ':'
                          + time_str + ":")
        fields.append(":(HGT):(surface):")

        # Create a temporary NetCDF file from the GRIB2 file.
        if(WGRIB2_env):
            cmd = '$WGRIB2 -match "(' + '|'.join(fields) + ')" ' + input_forcings.file_in2 + \
                  " -netcdf " + input_forcings.tmpFile
        else:
            cmd = '(' + '|'.join(fields) + ')'

        id_tmp = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFile, cmd,
                                  config_options, mpi_config, inputVar=None, special_case=False)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        create_link("WRF-ARW", input_forcings.file_in2, input_forcings.tmpFile, config_options, mpi_config)
        id_tmp = ioMod.open_netcdf_forcing(input_forcings.tmpFile, config_options, mpi_config)

    # Loop through all of the input forcings in NAM nest data. Convert the GRIB2 files
    # to NetCDF, read in the data, regrid it, then map it to the appropriate
    # array slice in the output arrays.
    for force_count, grib_var in enumerate(input_forcings.grib_vars):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing WRF-ARW Variable: " + grib_var
            err_handler.log_msg(config_options, mpi_config)

        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if calc_regrid_flag:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Calculating WRF-ARW regridding weights...."
                err_handler.log_msg(config_options, mpi_config)
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)
            err_handler.check_program_status(config_options, mpi_config)

            # Read in the RAP height field, which is used for downscaling purposes.
            # if mpi_config.rank == 0:
            #     config_options.statusMsg = "Reading in WRF-ARW elevation data from GRIB2."
            #     err_handler.log_msg(config_options, mpi_config)
            # cmd = "$WGRIB2 " + input_forcings.file_in2 + " -match " + \
            #       "\":(HGT):(surface):\" " + \
            #       " -netcdf " + input_forcings.tmpFileHeight
            # id_tmp_height = ioMod.open_grib2(input_forcings.file_in2, input_forcings.tmpFileHeight,
            #                                  cmd, config_options, mpi_config, 'HGT_surface')
            # err_handler.check_program_status(config_options, mpi_config)
            if(config_options.grid_type == "gridded"):
                # Regrid the height variable.
                if mpi_config.rank == 0:
                    var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                else:
                    var_tmp = None
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place NetCDF WRF-ARW elevation data into the ESMF field object: " \
                                        + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding WRF-ARW elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid WRF-ARW elevation data to the WRF-Hydro domain " \
                                            "using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to compute mask on WRF-ARW elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:, :] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF regridded WRF-ARW elevation data to a local " \
                                            "array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)
            elif(config_options.grid_type == "unstructured"):
                # Regrid the height variable.
                if mpi_config.rank == 0:
                    var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                else:
                    var_tmp = None
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place NetCDF WRF-ARW elevation data into the ESMF field object: " \
                                        + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding WRF-ARW elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid WRF-ARW elevation data to the WRF-Hydro domain " \
                                            "using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to compute mask on WRF-ARW elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF regridded WRF-ARW elevation data to a local " \
                                            "array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Regrid the height variable.
                if mpi_config.rank == 0:
                    var_tmp_elem = id_tmp.variables['HGT_surface'][0, :, :]
                else:
                    var_tmp_elem = None
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place NetCDF WRF-ARW elevation data into the ESMF field object: " \
                                        + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding WRF-ARW elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                             input_forcings.esmf_field_out_elem)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid WRF-ARW elevation data to the WRF-Hydro domain " \
                                            "using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to compute mask on WRF-ARW elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height_elem[:] = input_forcings.esmf_field_out_elem.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF regridded WRF-ARW elevation data to a local " \
                                            "array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            elif(config_options.grid_type == "hydrofabric"):
                # Regrid the height variable.
                if mpi_config.rank == 0:
                    var_tmp = id_tmp.variables['HGT_surface'][0, :, :]
                else:
                    var_tmp = None
                err_handler.check_program_status(config_options, mpi_config)

                var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to place NetCDF WRF-ARW elevation data into the ESMF field object: " \
                                        + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Regridding WRF-ARW elevation data to the WRF-Hydro domain."
                    err_handler.log_msg(config_options, mpi_config)
                try:
                    input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                             input_forcings.esmf_field_out)
                except ValueError as ve:
                    config_options.errMsg = "Unable to regrid WRF-ARW elevation data to the WRF-Hydro domain " \
                                            "using ESMF: " + str(ve)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                # Set any pixel cells outside the input domain to the global missing value.
                try:
                    input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                        config_options.globalNdv
                except (ValueError, ArithmeticError) as npe:
                    config_options.errMsg = "Unable to compute mask on WRF-ARW elevation data: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                try:
                    input_forcings.height[:] = input_forcings.esmf_field_out.data
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract ESMF regridded WRF-ARW elevation data to a local " \
                                            "array: " + str(err)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # Close the temporary NetCDF file and remove it.
            # if mpi_config.rank == 0:
            #     try:
            #         id_tmp_height.close()
            #     except OSError:
            #         config_options.errMsg = "Unable to close temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
            #
            #     try:
            #         os.remove(input_forcings.tmpFileHeight)
            #     except OSError:
            #         config_options.errMsg = "Unable to remove temporary file: " + input_forcings.tmpFileHeight
            #         err_handler.log_critical(config_options, mpi_config)
            # err_handler.check_program_status(config_options, mpi_config)

        err_handler.check_program_status(config_options, mpi_config)

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding WRF-ARW input variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input WRF-ARW forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input WRF-ARW regridded forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if grib_var == 'APCP':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != config_options.globalNdv)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on WRF ARW precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding WRF-ARW input variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input WRF-ARW forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input WRF-ARW regridded forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if grib_var == 'APCP':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != config_options.globalNdv)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on WRF ARW precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

            # Regrid the input variables.
            var_tmp_elem = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding WRF-ARW input variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp_elem = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input WRF-ARW forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input WRF-ARW regridded forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if grib_var == 'APCP':
                try:
                    ind_valid_elem = np.where(input_forcings.esmf_field_out_elem.data != config_options.globalNdv)
                    input_forcings.esmf_field_out_elem.data[ind_valid_elem] = input_forcings.esmf_field_out_elem.data[ind_valid_elem] / 3600.0
                    del ind_valid_elem
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on WRF ARW precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out_elem.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding WRF-ARW input variable: " + \
                                           input_forcings.netcdf_var_names[force_count]
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[force_count]][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract " + input_forcings.netcdf_var_names[force_count] + \
                                            " from: " + input_forcings.tmpFile + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input WRF-ARW forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input WRF-ARW regridded forcings: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if grib_var == 'APCP':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != config_options.globalNdv)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on WRF ARW precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
        try:
            os.remove(input_forcings.tmpFile)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + input_forcings.tmpFile
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)


def regrid_hourly_wrf_arw_hi_res_pcp(supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding hourly forecasted ARW precipitation for hi-res nests.
    :param supplemental_precip:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.exists(supplemental_precip.file_in1):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if supplemental_precip.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No ARW regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return

    # Create a path for a temporary NetCDF files that will
    # be created through the wgrib2 process.
    arw_tmp_nc = config_options.scratch_dir + "/ARW_PCP_TMP-{}.nc".format(mkfilename())

    if supplemental_precip.fileType != NETCDF:
        # These files shouldn't exist. If they do, remove them.
        if mpi_config.rank == 0:
            if os.path.isfile(arw_tmp_nc):
                config_options.statusMsg = "Found old temporary file: " + \
                                           arw_tmp_nc + " - Removing....."
                err_handler.log_warning(config_options, mpi_config)
                try:
                    os.remove(arw_tmp_nc)
                except IOError:
                    err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If the input paths have been set to None, this means input is missing. We will
        # alert the user, and set the final output grids to be the global NDV and return.
        # if not supplemental_precip.file_in1 or not supplemental_precip.file_in1:
        #    if MpiConfig.rank == 0:
        #        "NO ARW PRECIP AVAILABLE. SETTING FINAL SUPP GRIDS TO NDV"
        #    supplemental_precip.regridded_precip2 = None
        #    supplemental_precip.regridded_precip1 = None
        #    return
        # errMod.check_program_status(ConfigOptions, MpiConfig)

        # Create a temporary NetCDF file from the GRIB2 file.
        if(WGRIB2_env):
            cmd = "$WGRIB2 " + supplemental_precip.file_in1 + " -match \":(" + \
                  "APCP):(surface):(" + str(supplemental_precip.fcst_hour1 - 1) + \
                  "-" + str(supplemental_precip.fcst_hour1) + " hour acc fcst):\"" + \
                  " -netcdf " + arw_tmp_nc
        else:
            cmd = "(APCP):(surface):(" + str(supplemental_precip.fcst_hour1 - 1) + "-" + str(supplemental_precip.fcst_hour1) + " hour acc fcst)"

        id_tmp = ioMod.open_grib2(supplemental_precip.file_in1, arw_tmp_nc, cmd,
                                  config_options, mpi_config, "APCP_surface", special_case=False)
        err_handler.check_program_status(config_options, mpi_config)
    else:
        create_link("ARW-PCP", supplemental_precip.file_in1, arw_tmp_nc, config_options, mpi_config)
        id_tmp = ioMod.open_netcdf_forcing(arw_tmp_nc, config_options, mpi_config)

    # Check to see if we need to calculate regridding weights.
    calc_regrid_flag = check_supp_pcp_regrid_status(id_tmp, supplemental_precip, config_options,
                                                    wrf_hydro_geo_meta, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    if calc_regrid_flag:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Calculating WRF ARW regridding weights."
            err_handler.log_msg(config_options, mpi_config)
        calculate_supp_pcp_weights(supplemental_precip, id_tmp, arw_tmp_nc, config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

    if(config_options.grid_type == "gridded"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding WRF ARW APCP Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables['APCP_surface'][0, :, :]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from WRF ARW file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place WRF ARW precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid WRF ARW supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on WRF ARW supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:, :] = supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
            supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on WRF ARW supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:, :] = \
                supplemental_precip.regridded_precip2[:, :]
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "unstructured"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding WRF ARW APCP Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables['APCP_surface'][0, :, :]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from WRF ARW file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place WRF ARW precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid WRF ARW supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on WRF ARW supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:] = supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
            supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on WRF ARW supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:] = \
                supplemental_precip.regridded_precip2[:]
        err_handler.check_program_status(config_options, mpi_config)

        # Regrid the input variables.
        var_tmp_elem = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding WRF ARW APCP Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp_elem = id_tmp.variables['APCP_surface'][0, :, :]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from WRF ARW file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp_elem = mpi_config.scatter_array(supplemental_precip, var_tmp_elem, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place WRF ARW precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out_elem = supplemental_precip.regridObj_elem(supplemental_precip.esmf_field_in_elem,
                                                                               supplemental_precip.esmf_field_out_elem)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid WRF ARW supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out_elem.data[np.where(supplemental_precip.regridded_mask_elem == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on WRF ARW supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2_elem[:] = supplemental_precip.esmf_field_out_elem.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the hourly precipitation total to a rate of mm/s
        try:
            ind_valid_elem = np.where(supplemental_precip.regridded_precip2_elem != config_options.globalNdv)
            supplemental_precip.regridded_precip2_elem[ind_valid_elem] = supplemental_precip.regridded_precip2_elem[ind_valid_elem] / 3600.0
            del ind_valid_elem
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on WRF ARW supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1_elem[:] = \
                supplemental_precip.regridded_precip2_elem[:]
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "hydrofabric"):
        # Regrid the input variables.
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding WRF ARW APCP Precipitation."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables['APCP_surface'][0, :, :]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract precipitation from WRF ARW file: " + \
                                        supplemental_precip.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_in.data[:, :] = var_sub_tmp
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place WRF ARW precipitation into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                               supplemental_precip.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid WRF ARW supplemental precipitation: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value.
        try:
            supplemental_precip.esmf_field_out.data[np.where(supplemental_precip.regridded_mask == 0)] = \
                config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on WRF ARW supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_precip.regridded_precip2[:] = supplemental_precip.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # Convert the hourly precipitation total to a rate of mm/s
        try:
            ind_valid = np.where(supplemental_precip.regridded_precip2 != config_options.globalNdv)
            supplemental_precip.regridded_precip2[ind_valid] = supplemental_precip.regridded_precip2[ind_valid] / 3600.0
            del ind_valid
        except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
            config_options.errMsg = "Unable to run NDV search on WRF ARW supplemental precipitation: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_precip.regridded_precip1[:] = \
                supplemental_precip.regridded_precip2[:]
        err_handler.check_program_status(config_options, mpi_config)

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + arw_tmp_nc
            err_handler.log_critical(config_options, mpi_config)
        try:
            os.remove(arw_tmp_nc)
        except OSError:
            config_options.errMsg = "Unable to remove NetCDF file: " + arw_tmp_nc
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)


def regrid_sbcv2_liquid_water_fraction(supplemental_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding of SBCv2 Liquid Water Precip forcing files.
    :param input_forcings:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.exists(supplemental_forcings.file_in1):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if supplemental_forcings.regridComplete:
        return

    id_tmp = ioMod.open_netcdf_forcing(supplemental_forcings.file_in1, config_options, mpi_config)

    # Check to see if we need to calculate regridding weights.
    calc_regrid_flag = check_supp_pcp_regrid_status(id_tmp, supplemental_forcings, config_options,
                                                    wrf_hydro_geo_meta, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    if calc_regrid_flag:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Calculating SBCv2 Liquid Water Fraction regridding weights."
            err_handler.log_msg(config_options, mpi_config)
        calculate_supp_pcp_weights(supplemental_forcings, id_tmp, supplemental_forcings.file_in1,
                                   config_options, mpi_config, lat_var="Lat", lon_var="Lon")
        err_handler.check_program_status(config_options, mpi_config)

    if(config_options.grid_type == "gridded"):
        # Regrid the input variable
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding SBCv2 Liquid Water Fraction."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables[supplemental_forcings.netcdf_var_names[0]][:]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract Liquid Water Fraction from SBCv2 file: " + \
                                        supplemental_forcings.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_forcings, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_forcings.esmf_field_in.data[:, :] = var_sub_tmp / 100.0        # convert from 0-100 to 0-1.0
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place SBCv2 Liquid Water Fraction into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_forcings.esmf_field_out = supplemental_forcings.regridObj(supplemental_forcings.esmf_field_in,
                                                                                   supplemental_forcings.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid SBCv2 Liquid Water Fraction: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any missing data or pixel cells outside the input domain to a default of 100%
        try:
            supplemental_forcings.esmf_field_out.data[np.where(supplemental_forcings.regridded_mask == 0)] = 1.0
            supplemental_forcings.esmf_field_out.data[np.where(supplemental_forcings.esmf_field_out.data < 0)] = 1.0
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on SBCv2 Liquid Water Fraction: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_forcings.regridded_precip2[:] = supplemental_forcings.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_forcings.regridded_precip1[:] = \
                supplemental_forcings.regridded_precip2[:]
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "unstructured"):
        # Regrid the input variable
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding SBCv2 Liquid Water Fraction."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables[supplemental_forcings.netcdf_var_names[0]][:]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract Liquid Water Fraction from SBCv2 file: " + \
                                        supplemental_forcings.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_forcings, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_forcings.esmf_field_in.data[:, :] = var_sub_tmp / 100.0        # convert from 0-100 to 0-1.0
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place SBCv2 Liquid Water Fraction into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_forcings.esmf_field_out = supplemental_forcings.regridObj(supplemental_forcings.esmf_field_in,
                                                                                   supplemental_forcings.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid SBCv2 Liquid Water Fraction: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any missing data or pixel cells outside the input domain to a default of 100%
        try:
            supplemental_forcings.esmf_field_out.data[np.where(supplemental_forcings.regridded_mask == 0)] = 1.0
            supplemental_forcings.esmf_field_out.data[np.where(supplemental_forcings.esmf_field_out.data < 0)] = 1.0
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on SBCv2 Liquid Water Fraction: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_forcings.regridded_precip2[:] = supplemental_forcings.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_forcings.regridded_precip1[:] = \
                supplemental_forcings.regridded_precip2[:]
        err_handler.check_program_status(config_options, mpi_config)

        # Regrid the input variable
        var_tmp_elem = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding SBCv2 Liquid Water Fraction."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp_elem = id_tmp.variables[supplemental_forcings.netcdf_var_names[0]][:]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract Liquid Water Fraction from SBCv2 file: " + \
                                        supplemental_forcings.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp_elem = mpi_config.scatter_array(supplemental_forcings, var_tmp_elem, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem / 100.0        # convert from 0-100 to 0-1.0
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place SBCv2 Liquid Water Fraction into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_forcings.esmf_field_out_elem = supplemental_forcings.regridObj_elem(supplemental_forcings.esmf_field_in_elem,
                                                                                   supplemental_forcings.esmf_field_out_elem)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid SBCv2 Liquid Water Fraction: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any missing data or pixel cells outside the input domain to a default of 100%
        try:
            supplemental_forcings.esmf_field_out_elem.data[np.where(supplemental_forcings.regridded_mask_elem == 0)] = 1.0
            supplemental_forcings.esmf_field_out_elem.data[np.where(supplemental_forcings.esmf_field_out_elem.data < 0)] = 1.0
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on SBCv2 Liquid Water Fraction: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_forcings.regridded_precip2_elem[:] = supplemental_forcings.esmf_field_out_elem.data
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_forcings.regridded_precip1_elem[:] = \
                supplemental_forcings.regridded_precip2_elem[:]
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "hydrofabric"):
        # Regrid the input variable
        var_tmp = None
        if mpi_config.rank == 0:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding SBCv2 Liquid Water Fraction."
                err_handler.log_msg(config_options, mpi_config)
            try:
                var_tmp = id_tmp.variables[supplemental_forcings.netcdf_var_names[0]][:]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract Liquid Water Fraction from SBCv2 file: " + \
                                        supplemental_forcings.file_in1 + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(supplemental_forcings, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_forcings.esmf_field_in.data[:, :] = var_sub_tmp / 100.0        # convert from 0-100 to 0-1.0
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place SBCv2 Liquid Water Fraction into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            supplemental_forcings.esmf_field_out = supplemental_forcings.regridObj(supplemental_forcings.esmf_field_in,
                                                                                   supplemental_forcings.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to regrid SBCv2 Liquid Water Fraction: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any missing data or pixel cells outside the input domain to a default of 100%
        try:
            supplemental_forcings.esmf_field_out.data[np.where(supplemental_forcings.regridded_mask == 0)] = 1.0
            supplemental_forcings.esmf_field_out.data[np.where(supplemental_forcings.esmf_field_out.data < 0)] = 1.0
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = "Unable to run mask search on SBCv2 Liquid Water Fraction: " + str(npe)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        supplemental_forcings.regridded_precip2[:] = supplemental_forcings.esmf_field_out.data
        err_handler.check_program_status(config_options, mpi_config)

        # If we are on the first timestep, set the previous regridded field to be
        # the latest as there are no states for time 0.
        if config_options.current_output_step == 1:
            supplemental_forcings.regridded_precip1[:] = \
                supplemental_forcings.regridded_precip2[:]
        err_handler.check_program_status(config_options, mpi_config)

    # Close the NetCDF file
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + supplemental_forcings.file_in1
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)


def regrid_hourly_nbm(forcings_or_precip, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for handling regridding hourly forecasted NBM precipitation.
    :param forcings_or_precip:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # Do we want to use NBM data at this timestep? If not, log and continue
    if not config_options.use_data_at_current_time:
        if mpi_config.rank == 0:
            config_options.statusMsg = "Exceeded max hours for NBM data, will not use NBM in final layering."
            err_handler.log_msg(config_options, mpi_config)
        return
        
    # If the expected file is missing, this means we are allowing missing files, simply
    # exit out of this routine as the regridded fields have already been set to NDV.
    if not os.path.exists(forcings_or_precip.file_in1):
        return

    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if forcings_or_precip.regridComplete:
        return

    nbm_tmp_nc = config_options.scratch_dir + "/NBM_PCP_TMP-{}.nc".format(mkfilename())
    if mpi_config.rank == 0:
        if os.path.isfile(nbm_tmp_nc):
            config_options.statusMsg = "Found old temporary file: " + \
                                    nbm_tmp_nc + " - Removing....."
            err_handler.log_warning(config_options, mpi_config)
            try:
                os.remove(nbm_tmp_nc)
            except OSError:
                config_options.errMsg = "Unable to remove file: " + nbm_tmp_nc
                err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    if forcings_or_precip.grib_vars is not None:
        fields = []
        for force_count, grib_var in enumerate(forcings_or_precip.grib_vars):
            if mpi_config.rank == 0:
                config_options.statusMsg = "Converting NBM Variable: " + grib_var
                err_handler.log_msg(config_options, mpi_config)
            time_str = "{}-{} hour acc fcst".format(forcings_or_precip.fcst_hour1, forcings_or_precip.fcst_hour2) \
                if grib_var == 'APCP' else str(forcings_or_precip.fcst_hour2) + " hour fcst"
            fields.append(':' + grib_var + ':' +
                          forcings_or_precip.grib_levels[force_count] + ':'
                          + time_str + ":")
        # fields.append(":(HGT):(surface):")
        # Create a temporary NetCDF file from the GRIB2 file.
        cmd = '$WGRIB2 -match "(' + '|'.join(fields) + ')" -not "prob" -not "ens" ' + \
              forcings_or_precip.file_in1 + " -netcdf " + nbm_tmp_nc
    else:
        # Perform a GRIB dump to NetCDF for the precip data.
        fieldnbm_match1 = "\":APCP:\""
        fieldnbm_match2 = "\"" + str(forcings_or_precip.fcst_hour1) + "-" + str(forcings_or_precip.fcst_hour2) + "\""
        fieldnbm_notmatch1 = "\"prob\""  # We don't want the probabilistic QPF layers
        cmd = "$WGRIB2 " + forcings_or_precip.file_in1 + " -match " + fieldnbm_match1 \
                                                       + " -match " + fieldnbm_match2 \
                                                       + " -not " + fieldnbm_notmatch1 \
                                                       + " -netcdf " + nbm_tmp_nc

    id_tmp = ioMod.open_grib2(forcings_or_precip.file_in1, nbm_tmp_nc, cmd, config_options,
                              mpi_config, forcings_or_precip.netcdf_var_names[0],special_case=True)

    err_handler.check_program_status(config_options, mpi_config)

    for force_count, nc_var in enumerate(forcings_or_precip.netcdf_var_names):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing NBM Variable: " + nc_var
            err_handler.log_msg(config_options, mpi_config)

        # Check to see if we need to calculate regridding weights.
        is_supp = forcings_or_precip.grib_vars is None
        if is_supp:
            tag = "supplemental precip"
            calc_regrid_flag = check_supp_pcp_regrid_status(id_tmp, forcings_or_precip, config_options,
                                                            wrf_hydro_geo_meta, mpi_config)
        else:
            tag = "input"
            calc_regrid_flag = check_regrid_status(id_tmp, force_count, forcings_or_precip,
                                                   config_options, wrf_hydro_geo_meta, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if calc_regrid_flag:
            if is_supp:
                if mpi_config.rank == 0:
                    config_options.statusMsg = f"Calculating NBM {tag} regridding weights."
                    err_handler.log_msg(config_options, mpi_config)
                calculate_supp_pcp_weights(forcings_or_precip, id_tmp, forcings_or_precip.file_in1, config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)
            else:
                if mpi_config.rank == 0:
                    config_options.statusMsg = f"Calculating NBM {tag} regridding weights."
                    err_handler.log_msg(config_options, mpi_config)
                calculate_weights(id_tmp, force_count, forcings_or_precip, config_options, mpi_config, fill=True)
                err_handler.check_program_status(config_options, mpi_config)

                # Regrid the height variable.
                if config_options.grid_meta is None:
                    config_options.errMsg = "No NBM height file supplied, downscaling will not be available"
                    err_handler.log_warning(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)
                else:
                    if not os.path.exists(config_options.grid_meta):
                        config_options.errMsg = "NBM height file \"{config_options.grid_meta}\" does not exist"
                        err_handler.log_critical(config_options, mpi_config)
                    err_handler.check_program_status(config_options, mpi_config)

                    terrain_tmp = os.path.join(config_options.scratch_dir, 'nbm_terrain_temp.nc')
                    cmd = f"$WGRIB2 {config_options.grid_meta} -netcdf {terrain_tmp}"
                    hgt_tmp = ioMod.open_grib2(config_options.grid_meta, terrain_tmp, cmd, config_options,
                                               mpi_config, 'DIST_surface')
                    if(config_options.grid_type == "gridded"):
                        if mpi_config.rank == 0:
                            var_tmp = hgt_tmp.variables['DIST_surface'][0, :, :]
                        else:
                            var_tmp = None
                        err_handler.check_program_status(config_options, mpi_config)

                        var_sub_tmp = mpi_config.scatter_array(forcings_or_precip, var_tmp, config_options)
                        err_handler.check_program_status(config_options, mpi_config)

                        try:
                            forcings_or_precip.esmf_field_in.data[:, :] = var_sub_tmp
                        except (ValueError, KeyError, AttributeError) as err:
                            config_options.errMsg = "Unable to place NBM elevation data into the ESMF field object: " \
                                                + str(err)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        if mpi_config.rank == 0:
                            config_options.statusMsg = "Regridding NBM elevation data to the WRF-Hydro domain."
                            err_handler.log_msg(config_options, mpi_config)
                        try:
                            forcings_or_precip.esmf_field_out = forcings_or_precip.regridObj(forcings_or_precip.esmf_field_in,
                                                                                             forcings_or_precip.esmf_field_out)
                        except ValueError as ve:
                            config_options.errMsg = "Unable to regrid NBM elevation data to the WRF-Hydro domain " \
                                                    "using ESMF: " + str(ve)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        # Set any pixel cells outside the input domain to the global missing value.
                        try:
                            forcings_or_precip.esmf_field_out.data[np.where(forcings_or_precip.regridded_mask == 0)] = \
                                config_options.globalNdv
                        except (ValueError, ArithmeticError) as npe:
                            config_options.errMsg = "Unable to compute mask on NBM elevation data: " + str(npe)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        try:
                            forcings_or_precip.height[:, :] = forcings_or_precip.esmf_field_out.data
                        except (ValueError, KeyError, AttributeError) as err:
                            config_options.errMsg = "Unable to extract ESMF regridded NBM elevation data to a local " \
                                                    "array: " + str(err)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        if mpi_config.rank == 0:
                            hgt_tmp.close()
                    elif(config_options.grid_type == "hydrofabric"):
                        if mpi_config.rank == 0:
                            var_tmp = hgt_tmp.variables['DIST_surface'][0, :, :]
                        else:
                            var_tmp = None
                        err_handler.check_program_status(config_options, mpi_config)

                        var_sub_tmp = mpi_config.scatter_array(forcings_or_precip, var_tmp, config_options)
                        err_handler.check_program_status(config_options, mpi_config)

                        try:
                            forcings_or_precip.esmf_field_in.data[:, :] = var_sub_tmp
                        except (ValueError, KeyError, AttributeError) as err:
                            config_options.errMsg = "Unable to place NBM elevation data into the ESMF field object: " \
                                                + str(err)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        if mpi_config.rank == 0:
                            config_options.statusMsg = "Regridding NBM elevation data to the WRF-Hydro domain."
                            err_handler.log_msg(config_options, mpi_config)
                        try:
                            forcings_or_precip.esmf_field_out = forcings_or_precip.regridObj(forcings_or_precip.esmf_field_in,
                                                                                             forcings_or_precip.esmf_field_out)
                        except ValueError as ve:
                            config_options.errMsg = "Unable to regrid NBM elevation data to the WRF-Hydro domain " \
                                                    "using ESMF: " + str(ve)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        # Set any pixel cells outside the input domain to the global missing value.
                        try:
                            forcings_or_precip.esmf_field_out.data[np.where(forcings_or_precip.regridded_mask == 0)] = \
                                config_options.globalNdv
                        except (ValueError, ArithmeticError) as npe:
                            config_options.errMsg = "Unable to compute mask on NBM elevation data: " + str(npe)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        try:
                            forcings_or_precip.height[:] = forcings_or_precip.esmf_field_out.data
                        except (ValueError, KeyError, AttributeError) as err:
                            config_options.errMsg = "Unable to extract ESMF regridded NBM elevation data to a local " \
                                                    "array: " + str(err)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        if mpi_config.rank == 0:
                            hgt_tmp.close()
                    elif(config_options.grid_type == "unstructured"):
                        if mpi_config.rank == 0:
                            var_tmp = hgt_tmp.variables['DIST_surface'][0, :, :]
                        else:
                            var_tmp = None
                        err_handler.check_program_status(config_options, mpi_config)

                        var_sub_tmp = mpi_config.scatter_array(forcings_or_precip, var_tmp, config_options)
                        err_handler.check_program_status(config_options, mpi_config)

                        try:
                            forcings_or_precip.esmf_field_in.data[:, :] = var_sub_tmp
                        except (ValueError, KeyError, AttributeError) as err:
                            config_options.errMsg = "Unable to place NBM elevation data into the ESMF field object: " \
                                                + str(err)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        if mpi_config.rank == 0:
                            config_options.statusMsg = "Regridding NBM elevation data to the WRF-Hydro domain."
                            err_handler.log_msg(config_options, mpi_config)
                        try:
                            forcings_or_precip.esmf_field_out = forcings_or_precip.regridObj(forcings_or_precip.esmf_field_in,
                                                                                             forcings_or_precip.esmf_field_out)
                        except ValueError as ve:
                            config_options.errMsg = "Unable to regrid NBM elevation data to the WRF-Hydro domain " \
                                                    "using ESMF: " + str(ve)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        # Set any pixel cells outside the input domain to the global missing value.
                        try:
                            forcings_or_precip.esmf_field_out.data[np.where(forcings_or_precip.regridded_mask == 0)] = \
                                config_options.globalNdv
                        except (ValueError, ArithmeticError) as npe:
                            config_options.errMsg = "Unable to compute mask on NBM elevation data: " + str(npe)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        try:
                            forcings_or_precip.height[:] = forcings_or_precip.esmf_field_out.data
                        except (ValueError, KeyError, AttributeError) as err:
                            config_options.errMsg = "Unable to extract ESMF regridded NBM elevation data to a local " \
                                                    "array: " + str(err)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        if mpi_config.rank == 0:
                            var_tmp_elem = hgt_tmp.variables['DIST_surface'][0, :, :]
                        else:
                            var_tmp_elem = None
                        err_handler.check_program_status(config_options, mpi_config)

                        var_sub_tmp_elem = mpi_config.scatter_array(forcings_or_precip, var_tmp_elem, config_options)
                        err_handler.check_program_status(config_options, mpi_config)

                        try:
                            forcings_or_precip.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
                        except (ValueError, KeyError, AttributeError) as err:
                            config_options.errMsg = "Unable to place NBM elevation data into the ESMF field object: " \
                                                + str(err)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        if mpi_config.rank == 0:
                            config_options.statusMsg = "Regridding NBM elevation data to the unstructured domain."
                            err_handler.log_msg(config_options, mpi_config)
                        try:
                            forcings_or_precip.esmf_field_out_elem = forcings_or_precip.regridObj_elem(forcings_or_precip.esmf_field_in_elem,
                                                                                             forcings_or_precip.esmf_field_out_elem)
                        except ValueError as ve:
                            config_options.errMsg = "Unable to regrid NBM elevation data to the unstructured domain " \
                                                    "using ESMF: " + str(ve)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        # Set any pixel cells outside the input domain to the global missing value.
                        try:
                            forcings_or_precip.esmf_field_out_elem.data[np.where(forcings_or_precip.regridded_mask_elem == 0)] = \
                                config_options.globalNdv
                        except (ValueError, ArithmeticError) as npe:
                            config_options.errMsg = "Unable to compute element mask on NBM elevation data: " + str(npe)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        try:
                            forcings_or_precip.height_elem[:] = forcings_or_precip.esmf_field_out_elem.data
                        except (ValueError, KeyError, AttributeError) as err:
                            config_options.errMsg = "Unable to extract ESMF regridded NBM elevation data to a local " \
                                                    "array: " + str(err)
                            err_handler.log_critical(config_options, mpi_config)
                        err_handler.check_program_status(config_options, mpi_config)

                        if mpi_config.rank == 0:
                            hgt_tmp.close()

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding NBM {nc_var}"
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[nc_var][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract data from NBM file: " + \
                                            forcings_or_precip.file_in1 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(forcings_or_precip, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                forcings_or_precip.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = f"Unable to place NBM {tag} into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                forcings_or_precip.esmf_field_out = forcings_or_precip.regridObj(forcings_or_precip.esmf_field_in,
                                                                                 forcings_or_precip.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = f"Unable to regrid NBM {tag}: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                forcings_or_precip.esmf_field_out.data[np.where(forcings_or_precip.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask search on NBM supplemental precipitation: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            destination1 = forcings_or_precip.regridded_precip1 if is_supp else \
                forcings_or_precip.regridded_forcings1[forcings_or_precip.input_map_output[force_count]]

            destination2 = forcings_or_precip.regridded_precip2 if is_supp else \
                forcings_or_precip.regridded_forcings2[forcings_or_precip.input_map_output[force_count]]

            destination2[:, :] = forcings_or_precip.esmf_field_out.data
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total from kg.m-2.hour-1 to a rate of mm.s-1
            if 'APCP' in nc_var:
                try:
                    ind_valid = np.where(destination2 != config_options.globalNdv)
                    if forcings_or_precip.input_frequency == 60.0:
                        destination2[ind_valid] = destination2[ind_valid] / 3600.0
                    elif forcings_or_precip.input_frequency == 360.0:
                        destination2[ind_valid] = destination2[ind_valid] / 21600.0  # uniform disaggregation for 6-hourly nbm data
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on NBM precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                destination1[:, :] = \
                    destination2[:, :]
            err_handler.check_program_status(config_options, mpi_config)
        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding NBM {nc_var}"
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[nc_var][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract data from NBM file: " + \
                                            forcings_or_precip.file_in1 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(forcings_or_precip, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                forcings_or_precip.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = f"Unable to place NBM {tag} into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                forcings_or_precip.esmf_field_out = forcings_or_precip.regridObj(forcings_or_precip.esmf_field_in,
                                                                                 forcings_or_precip.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = f"Unable to regrid NBM {tag}: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # Set any pixel cells outside the input domain to the global missing value.
            try:
                forcings_or_precip.esmf_field_out.data[np.where(forcings_or_precip.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask search on NBM supplemental precipitation: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            destination1 = forcings_or_precip.regridded_precip1 if is_supp else \
                forcings_or_precip.regridded_forcings1[forcings_or_precip.input_map_output[force_count]]

            destination2 = forcings_or_precip.regridded_precip2 if is_supp else \
                forcings_or_precip.regridded_forcings2[forcings_or_precip.input_map_output[force_count]]

            destination2[:] = forcings_or_precip.esmf_field_out.data
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total from kg.m-2.hour-1 to a rate of mm.s-1
            if 'APCP' in nc_var:
                try:
                    ind_valid = np.where(destination2 != config_options.globalNdv)
                    if forcings_or_precip.input_frequency == 60.0:
                        destination2[ind_valid] = destination2[ind_valid] / 3600.0
                    elif forcings_or_precip.input_frequency == 360.0:
                        destination2[ind_valid] = destination2[ind_valid] / 21600.0  # uniform disaggregation for 6-hourly nbm data
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on NBM precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                destination1[:] = \
                    destination2[:]
            err_handler.check_program_status(config_options, mpi_config)
        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding NBM {nc_var}"
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp = id_tmp.variables[nc_var][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract data from NBM file: " + \
                                            forcings_or_precip.file_in1 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(forcings_or_precip, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                forcings_or_precip.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = f"Unable to place NBM {tag} into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                forcings_or_precip.esmf_field_out = forcings_or_precip.regridObj(forcings_or_precip.esmf_field_in,
                                                                                 forcings_or_precip.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = f"Unable to regrid NBM {tag}: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # Set any pixel cells outside the input domain to the global missing value.
            try:
                forcings_or_precip.esmf_field_out.data[np.where(forcings_or_precip.regridded_mask == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask search on NBM supplemental precipitation: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            destination1 = forcings_or_precip.regridded_precip1 if is_supp else \
                forcings_or_precip.regridded_forcings1[forcings_or_precip.input_map_output[force_count]]

            destination2 = forcings_or_precip.regridded_precip2 if is_supp else \
                forcings_or_precip.regridded_forcings2[forcings_or_precip.input_map_output[force_count]]

            destination2[:] = forcings_or_precip.esmf_field_out.data
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total from kg.m-2.hour-1 to a rate of mm.s-1
            if 'APCP' in nc_var:
                try:
                    ind_valid = np.where(destination2 != config_options.globalNdv)
                    if forcings_or_precip.input_frequency == 60.0:
                        destination2[ind_valid] = destination2[ind_valid] / 3600.0
                    elif forcings_or_precip.input_frequency == 360.0:
                        destination2[ind_valid] = destination2[ind_valid] / 21600.0  # uniform disaggregation for 6-hourly nbm data
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on NBM precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                destination1[:] = \
                    destination2[:]
            err_handler.check_program_status(config_options, mpi_config)

            # Regrid the input variables.
            var_tmp_elem = None
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Regridding NBM {nc_var}"
                err_handler.log_msg(config_options, mpi_config)
                try:
                    var_tmp_elem = id_tmp.variables[nc_var][0, :, :]
                except (ValueError, KeyError, AttributeError) as err:
                    config_options.errMsg = "Unable to extract data from NBM file: " + \
                                            forcings_or_precip.file_in1 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(forcings_or_precip, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                forcings_or_precip.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = f"Unable to place NBM {tag} into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                forcings_or_precip.esmf_field_out_elem = forcings_or_precip.regridObj_elem(forcings_or_precip.esmf_field_in_elem,
                                                                                 forcings_or_precip.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = f"Unable to regrid NBM {tag}: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)
            # Set any pixel cells outside the input domain to the global missing value.
            try:
                forcings_or_precip.esmf_field_out_elem.data[np.where(forcings_or_precip.regridded_mask_elem == 0)] = \
                    config_options.globalNdv
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to run mask search on NBM supplemental precipitation: " + str(npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            destination1_elem = forcings_or_precip.regridded_precip1_elem if is_supp else \
                forcings_or_precip.regridded_forcings1_elem[forcings_or_precip.input_map_output[force_count]]

            destination2_elem = forcings_or_precip.regridded_precip2_elem if is_supp else \
                forcings_or_precip.regridded_forcings2_elem[forcings_or_precip.input_map_output[force_count]]

            destination2_elem[:] = forcings_or_precip.esmf_field_out_elem.data
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total from kg.m-2.hour-1 to a rate of mm.s-1
            if 'APCP' in nc_var:
                try:
                    ind_valid_elem = np.where(destination2_elem != config_options.globalNdv)
                    if forcings_or_precip.input_frequency == 60.0:
                        destination2_elem[ind_valid_elem] = destination2_elem[ind_valid_elem] / 3600.0
                    elif forcings_or_precip.input_frequency == 360.0:
                        destination2_elem[ind_valid_elem] = destination2_elem[ind_valid_elem] / 21600.0  # uniform disaggregation for 6-hourly nbm data
                    del ind_valid_elem
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on NBM precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                destination1_elem[:] = \
                    destination2_elem[:]
            err_handler.check_program_status(config_options, mpi_config)

    # Close the temporary NetCDF file and remove it.
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + forcings_or_precip.file_in1
            err_handler.log_critical(config_options, mpi_config)
        try:
            os.remove(nbm_tmp_nc)
        except OSError:
            config_options.errMsg = "Unable to remove temporary NBM NetCDF file: " + nbm_tmp_nc
            err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

@static_vars(last_file=None)
def regrid_ndfd(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    # Check to see if the regrid complete flag for this
    # output time step is true. This entails the necessary
    # inputs have already been regridded and we can move on.
    if input_forcings.regridComplete:
        if mpi_config.rank == 0:
            config_options.statusMsg = "No NDFD regridding required for this timestep."
            err_handler.log_msg(config_options, mpi_config)
        return

    hour = input_forcings.fcst_hour2
    current_cycle = config_options.current_fcst_cycle
    forecast_time = config_options.current_time
    #DEBUG if mpi_config.rank == 0: print(f"NEXT FILE: {hour=}, {current_cycle=}, {forecast_time=}", flush=True)

    ndfd_files = ('tmp','wdir','wspd','qpf')
    fill_values = {'tmp': 288.0, 'wdir': 45.0, 'wspd': 0.71, 'qpf': 0}

    # check / set previous file to see if we're going to reuse
    reuse_prev_file = (f"{input_forcings.file_in2}-{current_cycle}" == regrid_ndfd.last_file)
    regrid_ndfd.last_file = f"{input_forcings.file_in2}-{current_cycle}"

    for i, ndfd_var in enumerate(ndfd_files):
        # check if file exists for this time period
        grb_file = input_forcings.file_in2.replace("%FIELD%", ndfd_var)

        tmp_file = os.path.join(config_options.scratch_dir, f"temp_ndfd_conus_{ndfd_var}.nc")
        # Temp file may exist. If it does, and we don't need it again, remove it.....
        if not reuse_prev_file and mpi_config.rank == 0:
            if os.path.isfile(tmp_file):
                config_options.statusMsg = f"Deleting old temporary file: {tmp_file}"
                err_handler.log_msg(config_options, mpi_config)
                try:
                    os.remove(tmp_file)
                except OSError:
                    config_options.errMsg = f"Unable to remove file: {tmp_file}"
                    err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if reuse_prev_file:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"Cycle unchanged, reusing temporary file: {tmp_file}"
                err_handler.log_msg(config_options, mpi_config)
            id_tmp = ioMod.open_netcdf_forcing(tmp_file, config_options, mpi_config)
        else:
            if mpi_config.rank == 0:
                config_options.statusMsg = f"New forecast cycle, creating temporary file from: {grb_file}, cycle hour {current_cycle.hour}"
                err_handler.log_msg(config_options, mpi_config)
            if(WGRIB2_env == False):
                cmd = [f":d={current_cycle.strftime('%Y%m%d%H')}:"]
            else:
                cmd = f"$WGRIB2 -match :d={current_cycle.strftime('%Y%m%d%H')}: -vt {grb_file} | " \
                    f"sort -t = -k 2 | $WGRIB2 -i -netcdf {tmp_file} {grb_file}"
            id_tmp = ioMod.open_grib2(grb_file, tmp_file, cmd, config_options, mpi_config, inputVar=None,special_case=True)

        # look to see if current time is in file:
        skip_file = np.ubyte(0)
        if mpi_config.rank == 0:
            times = [datetime.utcfromtimestamp(t) for t in id_tmp['time'][:]]
            if ndfd_var != "qpf":
                if forecast_time > times[-1]+timedelta(hours=3):
                    config_options.statusMsg = f"Forecast time beyond NDFD range, skipping"
                    err_handler.log_msg(config_options, mpi_config)
                    skip_file = 1
                elif forecast_time in times:
                    time_index = times.index(forecast_time)
                    config_options.statusMsg = f"Found exact time {forecast_time} in NDFD file at index {time_index} for variable {ndfd_var}"
                    err_handler.log_msg(config_options, mpi_config)
                else:
                    time_index = min(range(len(times)), key = lambda i: abs(times[i]-forecast_time))
                    config_options.statusMsg = f"Exact time {forecast_time} not found in NDFD file, using closest time at {times[time_index]}"
                    err_handler.log_msg(config_options, mpi_config)
            else:
                #TODO: qpf special handling
                if forecast_time > times[-1]-timedelta(hours=6):
                    config_options.statusMsg = f"Forecast time beyond NDFD precip range, skipping"
                    err_handler.log_msg(config_options, mpi_config)
                    skip_file = 1
                else:
                    time_index = int(hour // 6)
                    config_options.statusMsg = f"Forecast hour {forecast_time} will use precip from {times[time_index] - timedelta(hours=6)} to {times[time_index]}"
                    err_handler.log_msg(config_options, mpi_config)

        skip_file = mpi_config.broadcast_parameter(skip_file, config_options, param_type=np.ubyte)
        err_handler.check_program_status(config_options, mpi_config)

        if skip_file:
            if(config_options.grid_type == "gridded"):
                input_forcings.regridded_forcings2[input_forcings.input_map_output[i], :, :] = config_options.globalNdv
            elif(config_options.grid_type == "hydrofabric"):
                input_forcings.regridded_forcings2[input_forcings.input_map_output[i], :] = config_options.globalNdv
            elif(config_options.grid_type == "unstructured"):
                input_forcings.regridded_forcings2[input_forcings.input_map_output[i], :] = config_options.globalNdv
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[i], :] = config_options.globalNdv
            continue

        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing NDFD variable: " + ndfd_var
            err_handler.log_msg(config_options, mpi_config)

        calc_regrid_flag = check_regrid_status(id_tmp, i, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        if calc_regrid_flag:
            if mpi_config.rank == 0:
                config_options.statusMsg = "Calculating NDFD regridding weights."
                err_handler.log_msg(config_options, mpi_config)
            calculate_weights(id_tmp, i, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)
            err_handler.check_program_status(config_options, mpi_config)

        var_tmp = None
        if mpi_config.rank == 0:
            try:
                var_tmp = id_tmp.variables[input_forcings.netcdf_var_names[i]][time_index, :, :]
                var_tmp = var_tmp.filled(config_options.globalNdv)  # fill_values[ndfd_var])
                if(config_options.grid_type == "unstructured"):
                    var_tmp_elem = id_tmp.variables[input_forcings.netcdf_var_names[i]][time_index, :, :]
                    var_tmp_elem = var_tmp_elem.filled(config_options.globalNdv)
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = f"Unable to extract NDFD variable: {input_forcings.netcdf_var_names[i]} "  \
                                        f"from: {input_forcings.tmpFile} ({err})"
                err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
        err_handler.check_program_status(config_options, mpi_config)

        if(config_options.grid_type == "unstructured"):
            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)
        try:
            if(config_options.grid_type == "gridded"):
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            elif(config_options.grid_type == "hydrofabric"):
                input_forcings.esmf_field_in.data[:] = var_sub_tmp
            elif(config_options.grid_type == "unstructured"):
                input_forcings.esmf_field_in.data[:] = var_sub_tmp
                input_forcings.esmf_field_in_elem.data[:] = var_sub_tmp_elem
            #DEBUG if mpi_config.rank == 1: print(f"esmf_file_in has type: {type(input_forcings.esmf_field_in.data)}, var_sub_tmp has type: {type(var_sub_tmp)}")
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        try:
            input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                     input_forcings.esmf_field_out)
            if(config_options.grid_type == "unstructured"):
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
        except ValueError as ve:
            config_options.errMsg = f"Unable to regrid input NDFD forcing variable using ESMF: {ve}"
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Set any pixel cells outside the input domain to the global missing value, and fix missings that were interpolated
        try:
            input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = \
                config_options.globalNdv
            if(config_options.grid_type == "unstructured"):
                input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = \
                    config_options.globalNdv
            # input_forcings.esmf_field_out.data[np.where((input_forcings.esmf_field_out.data/config_options.globalNdv) > 0.75)] = \
            #     config_options.globalNdv
        except (ValueError, ArithmeticError) as npe:
            config_options.errMsg = f"Unable to calculate mask from input NDFD regridded forcings: {npe}"
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # If processing wind speed, calculate final U2 / V2 fields
        def u_v_comps(wspd, wdir):
            rads = wdir * (np.pi / 180.0)
            u = -1 * wspd * np.sin(rads)
            v = -1 * wspd * np.cos(rads)
            return u,v

        if ndfd_var == 'wspd':      # we already have direction at this point
            try:
                ind_valid = np.where(input_forcings.esmf_field_out.data != config_options.globalNdv)
                wspd = input_forcings.esmf_field_out.data[ind_valid]
                wdir = input_forcings.regridded_forcings2[input_forcings.input_map_output[i-1]][ind_valid]

                u, v = u_v_comps(wspd, wdir)
                input_forcings.regridded_forcings2[input_forcings.input_map_output[i-1]][ind_valid] = u
                input_forcings.esmf_field_out.data[ind_valid] = v

                if(config_options.grid_type == "unstructured"):
                    ind_valid = np.where(input_forcings.esmf_field_out_elem.data != config_options.globalNdv)
                    wspd = input_forcings.esmf_field_out_elem.data[ind_valid]
                    wdir = input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[i-1]][ind_valid]

                    u, v = u_v_comps(wspd, wdir)
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[i-1]][ind_valid] = u
                    input_forcings.esmf_field_out_elem.data[ind_valid] = v

                del ind_valid, u, v

            except (IndexError, ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                config_options.errMsg = f"Unable to convert NDFD wind speed/dir to U/V components: {npe}"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        # Convert the hourly precipitation total to a rate of mm/s
        if ndfd_var == 'qpf':
            try:
                ind_valid = np.where(input_forcings.esmf_field_out.data != config_options.globalNdv)
                input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[ind_valid] / 3600.0
                if(config_options.grid_type == "unstructured"):
                    ind_valid = np.where(input_forcings.esmf_field_out_elem.data != config_options.globalNdv)
                    input_forcings.esmf_field_out_elem.data[ind_valid] = input_forcings.esmf_field_out_elem.data[ind_valid] / 3600.0
                del ind_valid
            except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                config_options.errMsg = f"Unable to run NDV search on NDFD QPF precipitation: {npe}"
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

        try:
            if(config_options.grid_type == "gridded"):
                input_forcings.regridded_forcings2[input_forcings.input_map_output[i], :, :] = \
                    input_forcings.esmf_field_out.data
            elif(config_options.grid_type == "hydrofabric"):
                input_forcings.regridded_forcings2[input_forcings.input_map_output[i], :] = \
                    input_forcings.esmf_field_out.data
            elif(config_options.grid_type == "unstructured"):
                input_forcings.regridded_forcings2[input_forcings.input_map_output[i], :] = \
                    input_forcings.esmf_field_out.data
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[i], :] = \
                    input_forcings.esmf_field_out_elem.data
        except (ValueError, KeyError, AttributeError) as err:
            config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

def regrid_aorc_aws(input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):

    fill_values = {'TMP': 288.0, 'SPFH': 0.005, 'PRES': 101300.0, 'APCP': 0,
                   'UGRD': 1.0, 'VGRD': 1.0, 'DSWRF': 80.0, 'DLWRF': 310.0}
 
    mpi_config.comm.barrier()
    with MPICommExecutor(comm=mpi_config.comm, root=0) as executor:
        with dask.config.set(scheduler=executor):
            if mpi_config.rank == 0:
                xmax = np.max(wrf_hydro_geo_meta.lon_bounds)
                xmin = np.min(wrf_hydro_geo_meta.lon_bounds)
                ymax = np.max(wrf_hydro_geo_meta.lat_bounds)
                ymin = np.min(wrf_hydro_geo_meta.lat_bounds)
                id_tmp = config_options.aws_obj.sel(time=config_options.current_time.strftime('%Y-%m-%d %H:%M:%S'),longitude=slice(xmin, xmax),latitude=slice(ymin, ymax))
                id_tmp = id_tmp.compute()
            else:
                id_tmp = None
    mpi_config.comm.barrier()

    #if mpi_config.rank == 0:
    #    xmax = np.max(wrf_hydro_geo_meta.lon_bounds)
    #    xmin = np.min(wrf_hydro_geo_meta.lon_bounds)
    #    ymax = np.max(wrf_hydro_geo_meta.lat_bounds)
    #    ymin = np.min(wrf_hydro_geo_meta.lat_bounds)
    #    id_tmp = config_options.aws_obj.sel(time=config_options.current_time.strftime('%Y-%m-%d %H:%M:%S'),longitude=slice(xmin, xmax),latitude=slice(ymin, ymax))
    #    id_tmp = id_tmp.compute()
    #else:
    #    id_tmp = None
    #mpi_config.comm.barrier()

    for force_count, nc_var in enumerate(input_forcings.netcdf_var_names):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Processing Custom NetCDF Forcing Variable: " + nc_var
            err_handler.log_msg(config_options, mpi_config)
        calc_regrid_flag = check_regrid_status(id_tmp, force_count, input_forcings,
                                               config_options, wrf_hydro_geo_meta, mpi_config)

        if calc_regrid_flag:
            calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta)

        # Flag to set regridded mask for AORC to overlay with ERA5-Interim blend
        if(23 in config_options.input_forcings):
            input_forcings.regridded_mask_AORC = input_forcings.regridded_mask
            if(config_options.grid_type == "unstructured"):
                input_forcings.regridded_mask_elem_AORC = input_forcings.regridded_mask_elem

        if(config_options.grid_type == "gridded"):
            # Regrid the input variables.
            var_tmp = None
            fill = fill_values.get(input_forcings.grib_vars[force_count], config_options.globalNdv)
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using {fill} to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp = id_tmp[nc_var].to_masked_array().filled(fill)
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = fill
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input Custom netCDF regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'APCP_surface':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != fill)
                            # Flag to set regridded mask for AORC to overlay with ERA5-Interim blend
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[
                                                                        ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on Custom netCDF precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :, :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :, :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "unstructured"):
            # Regrid the input variables.
            var_tmp = None
            fill = fill_values.get(input_forcings.grib_vars[force_count], config_options.globalNdv)
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using {fill} to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp = id_tmp[nc_var].to_masked_array().filled(fill)
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = fill
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input Custom netCDF regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'APCP_surface':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != fill)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[
                                                                        ind_valid] / 3600.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on Custom netCDF precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

            # Regrid the input variables.
            var_tmp_elem = None
            fill = fill_values.get(input_forcings.grib_vars[force_count], config_options.globalNdv)
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using {fill} to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp_elem = id_tmp[nc_var].to_masked_array().filled(fill)
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp_elem = mpi_config.scatter_array(input_forcings, var_tmp_elem, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp_elem
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                         input_forcings.esmf_field_out_elem)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out_elem.data[np.where(input_forcings.regridded_mask_elem == 0)] = fill
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input Custom netCDF regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'APCP_surface':
                try:
                    ind_valid_elem = np.where(input_forcings.esmf_field_out_elem.data != fill)
                    input_forcings.esmf_field_out_elem.data[ind_valid_elem] = input_forcings.esmf_field_out_elem.data[
                                                                        ind_valid_elem] / 3600.0
                    del ind_valid_elem
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on Custom netCDF precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out_elem.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1_elem[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2_elem[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

        elif(config_options.grid_type == "hydrofabric"):
            # Regrid the input variables.
            var_tmp = None
            fill = fill_values.get(input_forcings.grib_vars[force_count], config_options.globalNdv)
            if mpi_config.rank == 0:
                config_options.statusMsg = "Regridding Custom netCDF input variable: " + nc_var
                err_handler.log_msg(config_options, mpi_config)
                try:
                    config_options.statusMsg = f"Using {fill} to replace missing values in input"
                    err_handler.log_msg(config_options, mpi_config)
                    var_tmp = id_tmp[nc_var].to_masked_array().filled(fill)
                except Exception as err:
                    config_options.errMsg = "Unable to extract " + nc_var + \
                                            " from: " + input_forcings.file_in2 + " (" + str(err) + ")"
                    err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local array into local ESMF field: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                         input_forcings.esmf_field_out)
            except ValueError as ve:
                config_options.errMsg = "Unable to regrid input Custom netCDF forcing variables using ESMF: " + str(ve)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Set any pixel cells outside the input domain to the global missing value.
            try:
                input_forcings.esmf_field_out.data[np.where(input_forcings.regridded_mask == 0)] = fill
            except (ValueError, ArithmeticError) as npe:
                config_options.errMsg = "Unable to calculate mask from input Custom netCDF regridded forcings: " + str(
                    npe)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # Convert the hourly precipitation total to a rate of mm/s
            if nc_var == 'APCP_surface':
                try:
                    ind_valid = np.where(input_forcings.esmf_field_out.data != fill)
                    input_forcings.esmf_field_out.data[ind_valid] = input_forcings.esmf_field_out.data[
                                                                        ind_valid] / 3600.0
                    ind_valid = np.where(input_forcings.esmf_field_out.data < 0.0)
                    input_forcings.esmf_field_out.data[ind_valid] = 0.0
                    del ind_valid
                except (ValueError, ArithmeticError, AttributeError, KeyError) as npe:
                    config_options.errMsg = "Unable to run NDV search on Custom netCDF precipitation: " + str(npe)
                    err_handler.log_critical(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

            try:
                input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.esmf_field_out.data
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to place local ESMF regridded data into local array: " + str(err)
                err_handler.log_critical(config_options, mpi_config)
            err_handler.check_program_status(config_options, mpi_config)

            # If we are on the first timestep, set the previous regridded field to be
            # the latest as there are no states for time 0.
            if config_options.current_output_step == 1:
                input_forcings.regridded_forcings1[input_forcings.input_map_output[force_count], :] = \
                    input_forcings.regridded_forcings2[input_forcings.input_map_output[force_count], :]
            err_handler.check_program_status(config_options, mpi_config)

    # Close the NetCDF file
    if mpi_config.rank == 0:
        try:
            id_tmp.close()
        except OSError:
            config_options.errMsg = "Unable to close NetCDF file: " + input_forcings.tmpFile
            err_handler.err_out(config_options)


         

def check_regrid_status(id_tmp, force_count, input_forcings, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for checking to see if regridding weights need to be
    calculated (or recalculated).
    :param wrf_hydro_geo_meta:
    :param force_count:
    :param id_tmp:
    :param input_forcings:
    :param config_options:
    :param mpi_config:
    :return:
    """
    # If the destination ESMF field hasn't been created, create it here.
    if not input_forcings.esmf_field_out:
        if(config_options.grid_type == 'gridded'):
            try:
                input_forcings.esmf_field_out = ESMF.Field(wrf_hydro_geo_meta.esmf_grid,
                                                           name=input_forcings.productName + 'FORCING_REGRIDDED')
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = "Unable to create " + input_forcings.productName + \
                                        " destination ESMF field object: " + str(esmf_error)
                err_handler.log_critical(config_options, mpi_config)

        elif(config_options.grid_type == 'unstructured'):
            try:
                input_forcings.esmf_field_out = ESMF.Field(wrf_hydro_geo_meta.esmf_grid,meshloc=ESMF.MeshLoc.NODE,name=input_forcings.productName + 'FORCING_REGRIDDED')
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = "Unable to create " + input_forcings.productName + \
                                        " destination ESMF field node mesh object: " + str(esmf_error)
                err_handler.log_critical(config_options, mpi_config)
            try:
                input_forcings.esmf_field_out_elem = ESMF.Field(wrf_hydro_geo_meta.esmf_grid,meshloc=ESMF.MeshLoc.ELEMENT,name=input_forcings.productName + 'FORCING_REGRIDDED')
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = "Unable to create " + input_forcings.productName + \
                                        " destination ESMF field element mesh object: " + str(esmf_error)
                err_handler.log_critical(config_options, mpi_config)
        elif(config_options.grid_type == 'hydrofabric'):
            try:
                input_forcings.esmf_field_out = ESMF.Field(wrf_hydro_geo_meta.esmf_grid,meshloc=ESMF.MeshLoc.ELEMENT,name=input_forcings.productName + 'FORCING_REGRIDDED')
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = "Unable to create " + input_forcings.productName + \
                                        " destination ESMF field element mesh object: " + str(esmf_error)
                err_handler.log_critical(config_options, mpi_config)

    err_handler.check_program_status(config_options, mpi_config)

    # Determine if we need to calculate a regridding object. The following situations warrant the calculation of
    # a new weight file:
    # 1.) This is the first output time step, so we need to calculate a weight file.
    # 2.) The input forcing grid has changed.
    calc_regrid_flag = False
    # mpi_config.comm.barrier()

    if input_forcings.nx_global is None or input_forcings.ny_global is None:
        # This is the first timestep.
        # Create out regridded numpy arrays to hold the regridded data.
        force_count = 9 if config_options.include_lqfrac else 8
        if(config_options.grid_type == 'gridded'):
            input_forcings.regridded_forcings1 = np.empty([force_count, wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                          np.float32)
            input_forcings.regridded_forcings2 = np.empty([force_count, wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                          np.float32)
        elif(config_options.grid_type == 'unstructured'):
            input_forcings.regridded_forcings1 = np.empty([force_count, wrf_hydro_geo_meta.ny_local], np.float32)
            input_forcings.regridded_forcings2 = np.empty([force_count, wrf_hydro_geo_meta.ny_local], np.float32)
            input_forcings.regridded_forcings1_elem = np.empty([force_count, wrf_hydro_geo_meta.ny_local_elem], np.float32)
            input_forcings.regridded_forcings2_elem = np.empty([force_count, wrf_hydro_geo_meta.ny_local_elem], np.float32)
        elif(config_options.grid_type == 'hydrofabric'):
            input_forcings.regridded_forcings1 = np.empty([force_count, wrf_hydro_geo_meta.ny_local], np.float32)
            input_forcings.regridded_forcings2 = np.empty([force_count, wrf_hydro_geo_meta.ny_local], np.float32)

    if mpi_config.rank == 0:
        if input_forcings.nx_global is None or input_forcings.ny_global is None:
            # This is the first timestep.
            calc_regrid_flag = True
        else:
            if mpi_config.rank == 0:
                if(config_options.aws):
                    if id_tmp.variables[input_forcings.netcdf_var_names[force_count]].shape[0] \
                            != input_forcings.ny_global and \
                            id_tmp.variables[input_forcings.netcdf_var_names[force_count]].shape[1] \
                            != input_forcings.nx_global:
                        calc_regrid_flag = True
                else:
                    if id_tmp.variables[input_forcings.netcdf_var_names[force_count]].shape[1] \
                            != input_forcings.ny_global and \
                            id_tmp.variables[input_forcings.netcdf_var_names[force_count]].shape[2] \
                            != input_forcings.nx_global:
                        calc_regrid_flag = True

    # mpi_config.comm.barrier()

    # Broadcast the flag to the other processors.
    calc_regrid_flag = mpi_config.broadcast_parameter(calc_regrid_flag, config_options, param_type=bool)
    err_handler.check_program_status(config_options, mpi_config)

    return calc_regrid_flag


def check_supp_pcp_regrid_status(id_tmp, supplemental_precip, config_options, wrf_hydro_geo_meta, mpi_config):
    """
    Function for checking to see if regridding weights need to be
    calculated (or recalculated).
    :param supplemental_precip:
    :param id_tmp:
    :param config_options:
    :param wrf_hydro_geo_meta:
    :param mpi_config:
    :return:
    """
    # If the destination ESMF field hasn't been created, create it here.
    if not supplemental_precip.esmf_field_out:
        if(config_options.grid_type == 'gridded'):
            try:
                supplemental_precip.esmf_field_out = ESMF.Field(wrf_hydro_geo_meta.esmf_grid,
                                                                name=supplemental_precip.productName + 'SUPP_PCP_REGRIDDED')
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = "Unable to create " + supplemental_precip.productName + \
                                        " destination ESMF field object: " + str(esmf_error)
                err_handler.err_out(config_options)
        elif(config_options.grid_type == 'unstructured'):
            try:
                supplemental_precip.esmf_field_out = ESMF.Field(wrf_hydro_geo_meta.esmf_grid,meshloc=ESMF.MeshLoc.NODE, name=supplemental_precip.productName + 'SUPP_PCP_REGRIDDED')
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = "Unable to create " + supplemental_precip.productName + \
                                        " destination ESMF node field object: " + str(esmf_error)
                err_handler.err_out(config_options)

            try:
                supplemental_precip.esmf_field_out_elem = ESMF.Field(wrf_hydro_geo_meta.esmf_grid,meshloc=ESMF.MeshLoc.ELEMENT, name=supplemental_precip.productName + 'SUPP_PCP_REGRIDDED')
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = "Unable to create " + supplemental_precip.productName + \
                                        " destination ESMF element field object: " + str(esmf_error)
                err_handler.err_out(config_options)

        elif(config_options.grid_type == 'hydrofabric'):
            try:
                supplemental_precip.esmf_field_out = ESMF.Field(wrf_hydro_geo_meta.esmf_grid,meshloc=ESMF.MeshLoc.ELEMENT, name=supplemental_precip.productName + 'SUPP_PCP_REGRIDDED')
            except ESMF.ESMPyException as esmf_error:
                config_options.errMsg = "Unable to create " + supplemental_precip.productName + \
                                        " destination ESMF element field object: " + str(esmf_error)
                err_handler.err_out(config_options)


    # Determine if we need to calculate a regridding object. The following situations warrant the calculation of
    # a new weight file:
    # 1.) This is the first output time step, so we need to calculate a weight file.
    # 2.) The input forcing grid has changed.
    calc_regrid_flag = False

    # mpi_config.comm.barrier()

    if supplemental_precip.nx_global is None or supplemental_precip.ny_global is None:
        if(config_options.grid_type == 'gridded'):
            # This is the first timestep.
            # Create out regridded numpy arrays to hold the regridded data.
            supplemental_precip.regridded_precip1 = np.empty([wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                             np.float32)
            supplemental_precip.regridded_precip2 = np.empty([wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                             np.float32)
            supplemental_precip.regridded_rqi1 = np.empty([wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                          np.float32)
            supplemental_precip.regridded_rqi2 = np.empty([wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                          np.float32)
            supplemental_precip.regridded_rqi1[:, :] = config_options.globalNdv
            supplemental_precip.regridded_rqi2[:, :] = config_options.globalNdv
        elif(config_options.grid_type == 'unstructured'):
            # This is the first timestep.
            # Create out regridded numpy arrays to hold the regridded data.
            supplemental_precip.regridded_precip1 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
            supplemental_precip.regridded_precip2 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
            supplemental_precip.regridded_rqi1 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
            supplemental_precip.regridded_rqi2 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
            supplemental_precip.regridded_rqi1[:] = config_options.globalNdv
            supplemental_precip.regridded_rqi2[:] = config_options.globalNdv
            supplemental_precip.regridded_precip1_elem = np.empty([wrf_hydro_geo_meta.ny_local_elem],np.float32)
            supplemental_precip.regridded_precip2_elem = np.empty([wrf_hydro_geo_meta.ny_local_elem],np.float32)
            supplemental_precip.regridded_rqi1_elem = np.empty([wrf_hydro_geo_meta.ny_local_elem],np.float32)
            supplemental_precip.regridded_rqi2_elem = np.empty([wrf_hydro_geo_meta.ny_local_elem],np.float32)
            supplemental_precip.regridded_rqi1_elem[:] = config_options.globalNdv
            supplemental_precip.regridded_rqi2_elem[:] = config_options.globalNdv

        elif(config_options.grid_type == 'hydrofabric'):
            # This is the first timestep.
            # Create out regridded numpy arrays to hold the regridded data.
            supplemental_precip.regridded_precip1 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
            supplemental_precip.regridded_precip2 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
            supplemental_precip.regridded_rqi1 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
            supplemental_precip.regridded_rqi2 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
            supplemental_precip.regridded_rqi1[:] = config_options.globalNdv
            supplemental_precip.regridded_rqi2[:] = config_options.globalNdv

    if mpi_config.rank == 0:
        if supplemental_precip.nx_global is None or supplemental_precip.ny_global is None:
            # This is the first timestep.
            calc_regrid_flag = True
        else:
            if mpi_config.rank == 0:
                ncvar = id_tmp.variables[supplemental_precip.netcdf_var_names[0]]
                ndims = len(ncvar.dimensions)
                if ndims == 2:
                    latdim = 0
                    londim = 1
                else:
                    latdim = 1
                    londim = 2
                if ncvar.shape[latdim] != supplemental_precip.ny_global and \
                        ncvar.shape[londim] != supplemental_precip.nx_global:
                    calc_regrid_flag = True

    if(config_options.grid_type == 'gridded'):
        # We will now check to see if the regridded arrays are still None. This means the fields were set to None
        # earlier for missing data. We need to reset them to nx_global/ny_global where the calc_regrid_flag is False.
        if supplemental_precip.regridded_precip2 is None:
            supplemental_precip.regridded_precip2 = np.empty([wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                             np.float32)
        if supplemental_precip.regridded_precip1 is None:
            supplemental_precip.regridded_precip1 = np.empty([wrf_hydro_geo_meta.ny_local, wrf_hydro_geo_meta.nx_local],
                                                             np.float32)
    elif(config_options.grid_type == 'unstructured'):
        # We will now check to see if the regridded arrays are still None. This means the fields were set to None
        # earlier for missing data. We need to reset them to nx_global/ny_global where the calc_regrid_flag is False.
        if supplemental_precip.regridded_precip2 is None:
            supplemental_precip.regridded_precip2 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
        if supplemental_precip.regridded_precip1 is None:
            supplemental_precip.regridded_precip1 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
        if supplemental_precip.regridded_precip2_elem is None:
            supplemental_precip.regridded_precip2_elem = np.empty([wrf_hydro_geo_meta.ny_local_elem],np.float32)
        if supplemental_precip.regridded_precip1_elem is None:
            supplemental_precip.regridded_precip1_elem = np.empty([wrf_hydro_geo_meta.ny_local_elem],np.float32)

    elif(config_options.grid_type == 'hydrofabric'):
        # We will now check to see if the regridded arrays are still None. This means the fields were set to None
        # earlier for missing data. We need to reset them to nx_global/ny_global where the calc_regrid_flag is False.
        if supplemental_precip.regridded_precip2 is None:
            supplemental_precip.regridded_precip2 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)
        if supplemental_precip.regridded_precip1 is None:
            supplemental_precip.regridded_precip1 = np.empty([wrf_hydro_geo_meta.ny_local],np.float32)


    # mpi_config.comm.barrier()

    # Broadcast the flag to the other processors.
    calc_regrid_flag = mpi_config.broadcast_parameter(calc_regrid_flag, config_options, param_type=bool)

    mpi_config.comm.barrier()
    return calc_regrid_flag


def calculate_weights(id_tmp, force_count, input_forcings, config_options, mpi_config, wrf_hydro_geo_meta,
                      lat_var="latitude", lon_var="longitude", fill=False):
    """
    Function to calculate ESMF weights based on the output ESMF
    field previously calculated, along with input lat/lon grids,
    and a sample dataset.
    :param input_forcings:
    :param id_tmp:
    :param mpi_config:
    :param config_options:
    :param force_count:
    :return:
    """

    if mpi_config.rank == 0:
        if(config_options.aws):
            try:
                input_forcings.ny_global = id_tmp.variables[input_forcings.netcdf_var_names[force_count]].shape[0]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract Y shape size from: " + \
                                        input_forcings.netcdf_var_names[force_count] + " from aws object"
                err_handler.log_critical(config_options, mpi_config)
            try:
                input_forcings.nx_global = id_tmp.variables[input_forcings.netcdf_var_names[force_count]].shape[1]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract X shape size from: " + \
                                        input_forcings.netcdf_var_names[force_count] + " from aws object"
                err_handler.log_critical(config_options, mpi_config)
        else:
            try:
                input_forcings.ny_global = id_tmp.variables[input_forcings.netcdf_var_names[force_count]].shape[1]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract Y shape size from: " + \
                                        input_forcings.netcdf_var_names[force_count] + " from: " + \
                                        input_forcings.tmpFile + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
            try:
                input_forcings.nx_global = id_tmp.variables[input_forcings.netcdf_var_names[force_count]].shape[2]
            except (ValueError, KeyError, AttributeError) as err:
                config_options.errMsg = "Unable to extract X shape size from: " + \
                                        input_forcings.netcdf_var_names[force_count] + " from: " + \
                                        input_forcings.tmpFile + " (" + str(err) + ")"
                err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    # Broadcast the forcing nx/ny values
    input_forcings.ny_global = mpi_config.broadcast_parameter(input_forcings.ny_global,
                                                              config_options, param_type=int)
    err_handler.check_program_status(config_options, mpi_config)
    input_forcings.nx_global = mpi_config.broadcast_parameter(input_forcings.nx_global,
                                                              config_options, param_type=int)
    err_handler.check_program_status(config_options, mpi_config)

    try:
        # noinspection PyTypeChecker
        input_forcings.esmf_grid_in = ESMF.Grid(np.array([input_forcings.ny_global, input_forcings.nx_global]),
                                                staggerloc=ESMF.StaggerLoc.CENTER,
                                                coord_sys=ESMF.CoordSys.SPH_DEG)
    except ESMF.ESMPyException as esmf_error:
        config_options.errMsg = "Unable to create source ESMF grid from temporary file: " + \
                                input_forcings.tmpFile + " (" + str(esmf_error) + ")"
        err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    try:
        input_forcings.x_lower_bound = input_forcings.esmf_grid_in.lower_bounds[ESMF.StaggerLoc.CENTER][1]
        input_forcings.x_upper_bound = input_forcings.esmf_grid_in.upper_bounds[ESMF.StaggerLoc.CENTER][1]
        input_forcings.y_lower_bound = input_forcings.esmf_grid_in.lower_bounds[ESMF.StaggerLoc.CENTER][0]
        input_forcings.y_upper_bound = input_forcings.esmf_grid_in.upper_bounds[ESMF.StaggerLoc.CENTER][0]
        input_forcings.nx_local = input_forcings.x_upper_bound - input_forcings.x_lower_bound
        input_forcings.ny_local = input_forcings.y_upper_bound - input_forcings.y_lower_bound
    except (ValueError, KeyError, AttributeError) as err:
        config_options.errMsg = "Unable to extract local X/Y boundaries from global grid from temporary " + \
                                "file: " + input_forcings.tmpFile + " (" + str(err) + ")"
        err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    # Check to make sure we have enough dimensionality to run regridding. ESMF requires both grids
    # to have a size of at least 2.
    if input_forcings.nx_local < 2 or input_forcings.ny_local < 2:
        config_options.errMsg = "You have either specified too many cores for: " + input_forcings.productName + \
                                ", or  your input forcing grid is too small to process. Local grid must " \
                                "have x/y dimension size of 2."
        err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    # check if we're doing border trimming and set up mask
    border = input_forcings.border  # // 5  # HRRR is a 3 km product
    if border > 0:
        try:
            mask = input_forcings.esmf_grid_in.add_item(ESMF.GridItem.MASK, ESMF.StaggerLoc.CENTER)
            if mpi_config.rank == 0:
                config_options.statusMsg = "Trimming input forcing `{}` by {} grid cells".format(
                        input_forcings.productName,
                        border)
                err_handler.log_msg(config_options, mpi_config)

            gmask = np.ones([input_forcings.ny_global, input_forcings.nx_global])
            gmask[:+border, :] = 0.  # top edge
            gmask[-border:, :] = 0.  # bottom edge
            gmask[:, :+border] = 0.  # left edge
            gmask[:, -border:] = 0.  # right edge

            mask[:, :] = mpi_config.scatter_array(input_forcings, gmask, config_options)
            err_handler.check_program_status(config_options, mpi_config)
        except Exception as e:
            print(e, flush=True)

    lat_tmp = None
    lon_tmp = None
    if mpi_config.rank == 0:
        if(input_forcings.productName == 'NWM'):
            nwm_geogrid = nc.Dataset(config_options.nwm_geogrid)
            lat_tmp = nwm_geogrid.variables['XLAT_M'][:][0,:,:]
            lon_tmp = nwm_geogrid.variables['XLONG_M'][:][0,:,:]
            nwm_geogrid.close()
        else:
            # Process lat/lon values from the GFS grid.
            if len(id_tmp.variables[lat_var].shape) == 3:
                # We have 2D grids already in place.
                lat_tmp = id_tmp.variables[lat_var][0, :, :]
                lon_tmp = id_tmp.variables[lon_var][0, :, :]
            elif len(id_tmp.variables[lon_var].shape) == 2:
                # We have 2D grids already in place.
                lat_tmp = id_tmp.variables[lat_var][:, :]
                lon_tmp = id_tmp.variables[lon_var][:, :]
            elif len(id_tmp.variables[lat_var].shape) == 1:
                # We have 1D lat/lons we need to translate into
                # 2D grids, one which would come from AORC AWS
                # s3 bucket data we need to flag here for
                if(config_options.aws):
                    lat_tmp = id_tmp.variables[lat_var][:].values
                    lon_tmp = id_tmp.variables[lon_var][:].values
                else:
                    lat_tmp = id_tmp.variables[lat_var][:]
                    lon_tmp = id_tmp.variables[lon_var][:]
                lon_tmp, lat_tmp = np.meshgrid(lon_tmp,lat_tmp)

    err_handler.check_program_status(config_options, mpi_config)

    # Scatter global GFS latitude grid to processors..
    if mpi_config.rank == 0:
        var_tmp = lat_tmp
    else:
        var_tmp = None
    var_sub_lat_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
    err_handler.check_program_status(config_options, mpi_config)

    if mpi_config.rank == 0:
        var_tmp = lon_tmp
    else:
        var_tmp = None
    var_sub_lon_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
    err_handler.check_program_status(config_options, mpi_config)

    try:
        input_forcings.esmf_lats = input_forcings.esmf_grid_in.get_coords(1)
    except ESMF.GridException as ge:
        config_options.errMsg = "Unable to locate latitude coordinate object within input ESMF grid: " + str(ge)
        err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    try:
        input_forcings.esmf_lons = input_forcings.esmf_grid_in.get_coords(0)
    except ESMF.GridException as ge:
        config_options.errMsg = "Unable to locate longitude coordinate object within input ESMF grid: " + str(ge)
        err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    input_forcings.esmf_lats[:, :] = var_sub_lat_tmp
    input_forcings.esmf_lons[:, :] = var_sub_lon_tmp
    del var_sub_lat_tmp
    del var_sub_lon_tmp
    del lat_tmp
    del lon_tmp

    if(config_options.grid_type == "gridded"):

        # Create a ESMF field to hold the incoming data.
        try:
            input_forcings.esmf_field_in = ESMF.Field(input_forcings.esmf_grid_in,
                                                      name=input_forcings.productName + "_NATIVE")
        except ESMF.ESMPyException as esmf_error:
            config_options.errMsg = "Unable to create ESMF field object: " + str(esmf_error)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

    if(config_options.grid_type == "unstructured"):
        
        # Create a ESMF field to hold the incoming data.
        try:
            input_forcings.esmf_field_in = ESMF.Field(input_forcings.esmf_grid_in,
                                                      name=input_forcings.productName + "_NATIVE")
        except ESMF.ESMPyException as esmf_error:
            config_options.errMsg = "Unable to create ESMF field object: " + str(esmf_error)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

        # Create a ESMF field to hold the incoming data.
        try:
            input_forcings.esmf_field_in_elem = ESMF.Field(input_forcings.esmf_grid_in,
                                                      name=input_forcings.productName + "_NATIVE_ELEMENT")
        except ESMF.ESMPyException as esmf_error:
            config_options.errMsg = "Unable to create ESMF field object: " + str(esmf_error)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

    elif(config_options.grid_type == "hydrofabric"):
        # Create a ESMF field to hold the incoming data.
        try:
            input_forcings.esmf_field_in = ESMF.Field(input_forcings.esmf_grid_in,
                                                      name=input_forcings.productName + "_NATIVE")
        except ESMF.ESMPyException as esmf_error:
            config_options.errMsg = "Unable to create ESMF field object: " + str(esmf_error)
            err_handler.log_critical(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)

    # Scatter global grid to processors..
    if mpi_config.rank == 0:
        if(config_options.aws):
            var_tmp = id_tmp[input_forcings.netcdf_var_names[force_count]].to_masked_array()
        else:
            var_tmp = id_tmp[input_forcings.netcdf_var_names[force_count]][0, :, :]
        # Set all valid values to 1, and all missing values to 0. This will
        # be used to generate an output mask that is used later on in downscaling, layering, etc.
        var_tmp.fill(1)
        var_tmp = var_tmp.filled(0)
    else:
        var_tmp = None
    var_sub_tmp = mpi_config.scatter_array(input_forcings, var_tmp, config_options)
    err_handler.check_program_status(config_options, mpi_config)

    if(config_options.grid_type == "gridded"):
        # Place temporary data into the field array for generating the regridding object.
        input_forcings.esmf_field_in.data[:, :] = var_sub_tmp

    elif(config_options.grid_type == "unstructured"):
        # Place temporary data into the field array for generating the regridding object.
        input_forcings.esmf_field_in.data[:, :] = var_sub_tmp
        input_forcings.esmf_field_in_elem.data[:, :] = var_sub_tmp

    elif(config_options.grid_type == "hydrofabric"):
        # Place temporary data into the field array for generating the regridding object.
        input_forcings.esmf_field_in.data[:, :] = var_sub_tmp

    # mpi_config.comm.barrier()

    # ## CALCULATE WEIGHT ## #
    # Try to find a pre-existing weight file, if available

    weight_file = None
    weight_file_elem = None
    if config_options.weightsDir is not None:
        grid_key = input_forcings.productName
        if(config_options.grid_type == "gridded"):
            weight_file = os.path.join(config_options.weightsDir, "ESMF_weight_{}_b{}.nc4".format(grid_key, border))
        elif(config_options.grid_type == "unstructured"):
            weight_file = os.path.join(config_options.weightsDir, "ESMF_weight_{}_b{}.nc4".format(grid_key, border))
            weight_file_elem = os.path.join(config_options.weightsDir, "ESMF_weight_{}_b{}_elem.nc4".format(grid_key, border))
        elif(config_options.grid_type == "hydrofabric"):
            weight_file = os.path.join(config_options.weightsDir, "ESMF_weight_{}_b{}.nc4".format(grid_key, border))

        # check if file exists:
        if (os.path.exists(weight_file)):
            # read the data
            try:
                if mpi_config.rank == 0:
                    config_options.statusMsg = "Loading cached ESMF weight object for " + input_forcings.productName + \
                                               " from " + weight_file
                    err_handler.log_msg(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                begin = time.monotonic()
                input_forcings.regridObj = ESMF.RegridFromFile(input_forcings.esmf_field_in,
                                                               input_forcings.esmf_field_out,
                                                               weight_file)
                end = time.monotonic()

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Finished loading weight object with ESMF, took {} seconds".format(
                        end - begin)
                    err_handler.log_msg(config_options, mpi_config)

            except (IOError, ValueError, ESMF.ESMPyException) as esmf_error:
                config_options.errMsg = "Unable to load cached ESMF weight file: " + str(esmf_error)
                err_handler.log_warning(config_options, mpi_config)

        # check if unstructured mesh element weight file exists:
        if (config_options.grid_type == "unstructured" and os.path.exists(weight_file_elem)):
            # read the data
            try:
                if mpi_config.rank == 0:
                    config_options.statusMsg = "Loading cached ESMF mesh element weight object for " + input_forcings.productName + \
                                               " from " + weight_file
                    err_handler.log_msg(config_options, mpi_config)
                err_handler.check_program_status(config_options, mpi_config)

                begin = time.monotonic()
                input_forcings.regridObj_elem = ESMF.RegridFromFile(input_forcings.esmf_field_in_elem,
                                                               input_forcings.esmf_field_out_elem,
                                                               weight_file_elem)
                end = time.monotonic()

                if mpi_config.rank == 0:
                    config_options.statusMsg = "Finished loading mesh element weight object with ESMF, took {} seconds".format(
                        end - begin)
                    err_handler.log_msg(config_options, mpi_config)

            except (IOError, ValueError, ESMF.ESMPyException) as esmf_error:
                config_options.errMsg = "Unable to load cached ESMF mesh element weight file: " + str(esmf_error)
                err_handler.log_warning(config_options, mpi_config)


    if (input_forcings.regridObj is None):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Creating weight object from ESMF"
            err_handler.log_msg(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)
        extrap_method = ESMF.ExtrapMethod.CREEP_FILL if fill else ESMF.ExtrapMethod.NONE
        regrid_method = (ESMF.RegridMethod.BILINEAR, ESMF.RegridMethod.NEAREST_STOD)[input_forcings.regridOpt - 1]
        try:
            begin = time.monotonic()
            input_forcings.regridObj = ESMF.Regrid(input_forcings.esmf_field_in,
                                                   input_forcings.esmf_field_out,
                                                   src_mask_values=np.array([0, config_options.globalNdv]),
                                                   regrid_method=regrid_method,
                                                   extrap_method=extrap_method,
                                                   unmapped_action=ESMF.UnmappedAction.IGNORE,
                                                   filename=weight_file)
            end = time.monotonic()

            if mpi_config.rank == 0:
                config_options.statusMsg = "Finished generating weight object with ESMF, took {} seconds".format(
                        end - begin)
                err_handler.log_msg(config_options, mpi_config)
        except (RuntimeError, ImportError, ESMF.ESMPyException) as esmf_error:
            config_options.errMsg = "Unable to regrid input data from ESMF: " + str(esmf_error)
            err_handler.log_critical(config_options, mpi_config)
            etype, value, tb = sys.exc_info()
            traceback.print_exception(etype, value, tb)

        err_handler.check_program_status(config_options, mpi_config)

        # Run the regridding object on this test dataset. Check the output grid for
        # any 0 values.
        try:
            input_forcings.esmf_field_out = input_forcings.regridObj(input_forcings.esmf_field_in,
                                                                     input_forcings.esmf_field_out)
        except ValueError as ve:
            config_options.errMsg = "Unable to extract regridded data from ESMF regridded field: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
            # delete bad cached file if it exists
            if weight_file is not None:
                if os.path.exists(weight_file):
                    os.remove(weight_file)
        err_handler.check_program_status(config_options, mpi_config)

    if (config_options.grid_type == "gridded"):
        input_forcings.regridded_mask[:, :] = np.round(input_forcings.esmf_field_out.data[:, :])
    elif(config_options.grid_type != "gridded"):
        input_forcings.regridded_mask[:] = np.round(input_forcings.esmf_field_out.data[:])

    if (config_options.grid_type == "unstructured" and input_forcings.regridObj_elem is None):
        if mpi_config.rank == 0:
            config_options.statusMsg = "Creating ESMF mesh element weight object from ESMF"
            err_handler.log_msg(config_options, mpi_config)
        err_handler.check_program_status(config_options, mpi_config)
        extrap_method = ESMF.ExtrapMethod.CREEP_FILL if fill else ESMF.ExtrapMethod.NONE
        regrid_method = (ESMF.RegridMethod.BILINEAR, ESMF.RegridMethod.NEAREST_STOD)[input_forcings.regridOpt - 1]
        try:
            begin = time.monotonic()
            input_forcings.regridObj_elem = ESMF.Regrid(input_forcings.esmf_field_in_elem,
                                                   input_forcings.esmf_field_out_elem,
                                                   src_mask_values=np.array([0, config_options.globalNdv]),
                                                   regrid_method=regrid_method,
                                                   extrap_method=extrap_method,
                                                   unmapped_action=ESMF.UnmappedAction.IGNORE,
                                                   filename=weight_file_elem)
            end = time.monotonic()

            if mpi_config.rank == 0:
                config_options.statusMsg = "Finished generating ESMF mesh element weight object with ESMF, took {} seconds".format(
                        end - begin)
                err_handler.log_msg(config_options, mpi_config)
        except (RuntimeError, ImportError, ESMF.ESMPyException) as esmf_error:
            config_options.errMsg = "Unable to regrid input data into ESMF mesh element weight object from ESMF: " + str(esmf_error)
            err_handler.log_critical(config_options, mpi_config)
            etype, value, tb = sys.exc_info()
            traceback.print_exception(etype, value, tb)

        err_handler.check_program_status(config_options, mpi_config)

        # Run the regridding object on this test dataset. Check the output grid for
        # any 0 values.
        try:
            input_forcings.esmf_field_out_elem = input_forcings.regridObj_elem(input_forcings.esmf_field_in_elem,
                                                                     input_forcings.esmf_field_out_elem)
        except ValueError as ve:
            config_options.errMsg = "Unable to extract regridded data from ESMF regridded field: " + str(ve)
            err_handler.log_critical(config_options, mpi_config)
            # delete bad cached file if it exists
            if weight_file_elem is not None:
                if os.path.exists(weight_file_elem):
                    os.remove(weight_file_elem)
        err_handler.check_program_status(config_options, mpi_config)

        input_forcings.regridded_mask_elem[:] = np.round(input_forcings.esmf_field_out_elem.data[:])

def calculate_supp_pcp_weights(supplemental_precip, id_tmp, tmp_file, config_options, mpi_config,
                               lat_var="latitude", lon_var="longitude"):
    """
    Function to calculate ESMF weights based on the output ESMF
    field previously calculated, along with input lat/lon grids,
    and a sample dataset.
    :param tmp_file:
    :param id_tmp:
    :param supplemental_precip:
    :param mpi_config:
    :param config_options:
    :return:
    """
    ndims = 0
    if mpi_config.rank == 0:
        ncvar = id_tmp.variables[supplemental_precip.netcdf_var_names[0]]
        ndims = len(ncvar.dimensions)
        if ndims == 3:
            latdim = 1
            londim = 2
        elif ndims == 2:
            latdim = 0
            londim = 1
        else:
            latdim = londim = -1
            config_options.errMsg = "Unable to determine lat/lon grid size from " + tmp_file
            err_handler.err_out(config_options)

        try:
            supplemental_precip.ny_global = id_tmp.variables[supplemental_precip.netcdf_var_names[0]].shape[latdim]
        except (ValueError, KeyError, AttributeError, Exception) as err:
            config_options.errMsg = "Unable to extract Y shape size from: " + \
                                    supplemental_precip.netcdf_var_names[0] + " from: " + \
                                    tmp_file + " (" + str(err) + ", " + type(err) + ")"
            err_handler.err_out(config_options)
        try:
            supplemental_precip.nx_global = id_tmp.variables[supplemental_precip.netcdf_var_names[0]].shape[londim]
        except (ValueError, KeyError, AttributeError, Exception) as err:
            config_options.errMsg = "Unable to extract X shape size from: " + \
                                    supplemental_precip.netcdf_var_names[0] + " from: " + \
                                    tmp_file + " (" + str(err) + ", " + type(err) + ")"
            err_handler.err_out(config_options)

    # mpi_config.comm.barrier()

    # Broadcast the forcing nx/ny values
    supplemental_precip.ny_global = mpi_config.broadcast_parameter(supplemental_precip.ny_global,
                                                                   config_options, param_type=int)
    supplemental_precip.nx_global = mpi_config.broadcast_parameter(supplemental_precip.nx_global,
                                                                   config_options, param_type=int)
    # mpi_config.comm.barrier()

    try:
        # noinspection PyTypeChecker
        supplemental_precip.esmf_grid_in = ESMF.Grid(np.array([supplemental_precip.ny_global,
                                                               supplemental_precip.nx_global]),
                                                     staggerloc=ESMF.StaggerLoc.CENTER,
                                                     coord_sys=ESMF.CoordSys.SPH_DEG)
    except ESMF.ESMPyException as esmf_error:
        config_options.errMsg = "Unable to create source ESMF grid from temporary file: " + \
                                tmp_file + " (" + str(esmf_error) + ")"
        err_handler.err_out(config_options)
    # mpi_config.comm.barrier()

    try:
        supplemental_precip.x_lower_bound = supplemental_precip.esmf_grid_in.lower_bounds[ESMF.StaggerLoc.CENTER][1]
        supplemental_precip.x_upper_bound = supplemental_precip.esmf_grid_in.upper_bounds[ESMF.StaggerLoc.CENTER][1]
        supplemental_precip.y_lower_bound = supplemental_precip.esmf_grid_in.lower_bounds[ESMF.StaggerLoc.CENTER][0]
        supplemental_precip.y_upper_bound = supplemental_precip.esmf_grid_in.upper_bounds[ESMF.StaggerLoc.CENTER][0]
        supplemental_precip.nx_local = supplemental_precip.x_upper_bound - supplemental_precip.x_lower_bound
        supplemental_precip.ny_local = supplemental_precip.y_upper_bound - supplemental_precip.y_lower_bound
    except (ValueError, KeyError, AttributeError) as err:
        config_options.errMsg = "Unable to extract local X/Y boundaries from global grid from temporary " + \
                                "file: " + tmp_file + " (" + str(err) + ")"
        err_handler.err_out(config_options)
    # mpi_config.comm.barrier()

    # Check to make sure we have enough dimensionality to run regridding. ESMF requires both grids
    # to have a size of at least 2.
    if supplemental_precip.nx_local < 2 or supplemental_precip.ny_local < 2:
        config_options.errMsg = "You have either specified too many cores for: " + supplemental_precip.productName + \
                                ", or  your input forcing grid is too small to process. Local grid " \
                                "must have x/y dimension size of 2."
        err_handler.log_critical(config_options, mpi_config)
    err_handler.check_program_status(config_options, mpi_config)

    lat_tmp = lon_tmp = None
    if mpi_config.rank == 0:
        # Process lat/lon values from the GFS grid.
        if len(id_tmp.variables[lat_var].shape) == 3:
            # We have 2D grids already in place.
            lat_tmp = id_tmp.variables[lat_var][0, :]
            lon_tmp = id_tmp.variables[lon_var][0, :]
        elif len(id_tmp.variables[lon_var].shape) == 2:
            # We have 2D grids already in place.
            lat_tmp = id_tmp.variables[lat_var][:]
            lon_tmp = id_tmp.variables[lon_var][:]
        elif len(id_tmp.variables[lat_var].shape) == 1:
            # We have 1D lat/lons we need to translate into
            # 2D grids.
            lat_tmp = np.repeat(id_tmp.variables[lat_var][:][:, np.newaxis], supplemental_precip.nx_global, axis=1)
            lon_tmp = np.tile(id_tmp.variables[lon_var][:], (supplemental_precip.ny_global, 1))
    # mpi_config.comm.barrier()

    # Scatter global GFS latitude grid to processors..
    if mpi_config.rank == 0:
        var_tmp = lat_tmp
    else:
        var_tmp = None
    var_sub_lat_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
    # mpi_config.comm.barrier()

    if mpi_config.rank == 0:
        var_tmp = lon_tmp
    else:
        var_tmp = None
    var_sub_lon_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
    # mpi_config.comm.barrier()

    try:
        supplemental_precip.esmf_lats = supplemental_precip.esmf_grid_in.get_coords(1)
    except ESMF.GridException as ge:
        config_options.errMsg = "Unable to locate latitude coordinate object within supplemental precip ESMF grid: " \
                                + str(ge)
        err_handler.err_out(config_options)
    # mpi_config.comm.barrier()

    try:
        supplemental_precip.esmf_lons = supplemental_precip.esmf_grid_in.get_coords(0)
    except ESMF.GridException as ge:
        config_options.errMsg = "Unable to locate longitude coordinate object within supplemental precip ESMF grid: " \
                                + str(ge)
        err_handler.err_out(config_options)
    # mpi_config.comm.barrier()

    supplemental_precip.esmf_lats[:, :] = var_sub_lat_tmp
    supplemental_precip.esmf_lons[:, :] = var_sub_lon_tmp
    del var_sub_lat_tmp
    del var_sub_lon_tmp
    del lat_tmp
    del lon_tmp

    if (config_options.grid_type == "gridded"):
        # Create a ESMF field to hold the incoming data.
        supplemental_precip.esmf_field_in = ESMF.Field(supplemental_precip.esmf_grid_in,
                                                       name=supplemental_precip.productName + "_NATIVE")

        # mpi_config.comm.barrier()

        # Scatter global grid to processors..
        if mpi_config.rank == 0:
            if ndims == 3:
                var_tmp = id_tmp[supplemental_precip.netcdf_var_names[0]][0, :]
            elif ndims == 2:
                var_tmp = id_tmp[supplemental_precip.netcdf_var_names[0]][:]
            else:
                var_tmp = None
            # Set all valid values to 1.0, and all missing values to 0.0. This will
            # be used to generate an output mask that is used later on in downscaling, layering,
            # etc.
            var_tmp[:] = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,1.0)
        else:
            var_tmp = None
        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        mpi_config.comm.barrier()

        # Place temporary data into the field array for generating the regridding object.
        supplemental_precip.esmf_field_in.data[:] = var_sub_tmp
        # mpi_config.comm.barrier()

        supplemental_precip.regridObj = ESMF.Regrid(supplemental_precip.esmf_field_in,
                                                    supplemental_precip.esmf_field_out,
                                                    src_mask_values=np.array([0]),
                                                    regrid_method=ESMF.RegridMethod.BILINEAR,
                                                    unmapped_action=ESMF.UnmappedAction.IGNORE)

        # Run the regridding object on this test dataset. Check the output grid for
        # any 0 values.
        supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                           supplemental_precip.esmf_field_out)
        supplemental_precip.regridded_mask[:] = supplemental_precip.esmf_field_out.data[:]

    elif (config_options.grid_type == "unstructured"):
        # Create a ESMF field to hold the incoming data.
        supplemental_precip.esmf_field_in = ESMF.Field(supplemental_precip.esmf_grid_in,
                                                       name=supplemental_precip.productName + "_NATIVE")

        # mpi_config.comm.barrier()

        # Scatter global grid to processors..
        if mpi_config.rank == 0:
            if ndims == 3:
                var_tmp = id_tmp[supplemental_precip.netcdf_var_names[0]][0, :].data
            elif ndims == 2:
                var_tmp = id_tmp[supplemental_precip.netcdf_var_names[0]][:].data
            else:
                var_tmp = None
            # Set all valid values to 1.0, and all missing values to 0.0. This will
            # be used to generate an output mask that is used later on in downscaling, layering,
            # etc.
            var_tmp[:] = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,1.0)
        else:
            var_tmp = None
        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        mpi_config.comm.barrier()

        # Place temporary data into the field array for generating the regridding object.
        supplemental_precip.esmf_field_in.data[:] = var_sub_tmp
        # mpi_config.comm.barrier()

        supplemental_precip.regridObj = ESMF.Regrid(supplemental_precip.esmf_field_in,
                                                    supplemental_precip.esmf_field_out,
                                                    src_mask_values=np.array([0]),
                                                    regrid_method=ESMF.RegridMethod.BILINEAR,
                                                    unmapped_action=ESMF.UnmappedAction.IGNORE)

        # Run the regridding object on this test dataset. Check the output grid for
        # any 0 values.
        supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                           supplemental_precip.esmf_field_out)
        supplemental_precip.regridded_mask[:] = supplemental_precip.esmf_field_out.data[:]

        # Create a ESMF field to hold the incoming data.
        supplemental_precip.esmf_field_in_elem = ESMF.Field(supplemental_precip.esmf_grid_in,
                                                       name=supplemental_precip.productName + "_NATIVE")

        # mpi_config.comm.barrier()

        # Scatter global grid to processors..
        if mpi_config.rank == 0:
            if ndims == 3:
                var_tmp_elem = id_tmp[supplemental_precip.netcdf_var_names[0]][0, :].data
            elif ndims == 2:
                var_tmp_elem = id_tmp[supplemental_precip.netcdf_var_names[0]][:].data
            else:
                var_tmp_elem = None
            # Set all valid values to 1.0, and all missing values to 0.0. This will
            # be used to generate an output mask that is used later on in downscaling, layering,
            # etc.
            var_tmp[:] = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,1.0)
        else:
            var_tmp_elem = None
        var_sub_tmp_elem = mpi_config.scatter_array(supplemental_precip, var_tmp_elem, config_options)
        mpi_config.comm.barrier()

        # Place temporary data into the field array for generating the regridding object.
        supplemental_precip.esmf_field_in_elem.data[:] = var_sub_tmp_elem
        # mpi_config.comm.barrier()

        supplemental_precip.regridObj_elem = ESMF.Regrid(supplemental_precip.esmf_field_in_elem,
                                                    supplemental_precip.esmf_field_out_elem,
                                                    src_mask_values=np.array([0]),
                                                    regrid_method=ESMF.RegridMethod.BILINEAR,
                                                    unmapped_action=ESMF.UnmappedAction.IGNORE)

        # Run the regridding object on this test dataset. Check the output grid for
        # any 0 values.
        supplemental_precip.esmf_field_out_elem = supplemental_precip.regridObj_elem(supplemental_precip.esmf_field_in_elem,
                                                                           supplemental_precip.esmf_field_out_elem)
        supplemental_precip.regridded_mask_elem[:] = supplemental_precip.esmf_field_out_elem.data[:]

    elif (config_options.grid_type == "hydrofabric"):
        # Create a ESMF field to hold the incoming data.
        supplemental_precip.esmf_field_in = ESMF.Field(supplemental_precip.esmf_grid_in,
                                                       name=supplemental_precip.productName + "_NATIVE")

        # mpi_config.comm.barrier()

        # Scatter global grid to processors..
        if mpi_config.rank == 0:
            if ndims == 3:
                var_tmp = id_tmp[supplemental_precip.netcdf_var_names[0]][0, :]
            elif ndims == 2:
                var_tmp = id_tmp[supplemental_precip.netcdf_var_names[0]][:]
            else:
                var_tmp = None
            # Set all valid values to 1.0, and all missing values to 0.0. This will
            # be used to generate an output mask that is used later on in downscaling, layering,
            # etc.
            var_tmp[:] = np.where(var_tmp==id_tmp[supplemental_precip.netcdf_var_names[0]]._FillValue,0.0,1.0)
            var_tmp[:] = np.where(var_tmp==9.999e20,0.0,1.0)
        else:
            var_tmp = None
        var_sub_tmp = mpi_config.scatter_array(supplemental_precip, var_tmp, config_options)
        mpi_config.comm.barrier()

        # Place temporary data into the field array for generating the regridding object.
        supplemental_precip.esmf_field_in.data[:] = var_sub_tmp
        # mpi_config.comm.barrier()
        supplemental_precip.regridObj = ESMF.Regrid(supplemental_precip.esmf_field_in,
                                                    supplemental_precip.esmf_field_out,
                                                    src_mask_values=np.array([0]),
                                                    regrid_method=ESMF.RegridMethod.BILINEAR,
                                                    unmapped_action=ESMF.UnmappedAction.IGNORE,extrap_method=ESMF.ExtrapMethod.NEAREST_STOD)

        # Run the regridding object on this test dataset. Check the output grid for
        # any 0 values.
        supplemental_precip.esmf_field_out = supplemental_precip.regridObj(supplemental_precip.esmf_field_in,
                                                                           supplemental_precip.esmf_field_out)
        supplemental_precip.regridded_mask[:] = supplemental_precip.esmf_field_out.data[:]


