Sub Main()
    BuildProject "E:/efficient_adjoint/cst/coupler_fwd.cst", 1
    BuildProject "E:/efficient_adjoint/cst/coupler_bwd.cst", 3
End Sub

Sub BuildProject(fname As String, portnum As Integer)
    NewProject
    With Units
        .Geometry "mm"
        .Frequency "GHz"
        .Time "ns"
    End With
With Material
    .Reset
    .Name "Rogers4350B"
    .Folder ""
    .FrqType "all"
    .Type "Normal"
    .SetMaterialUnit "GHz", "mm"
    .Epsilon "3.66"
    .Mu "1.0"
    .TanD "0.0037"
    .Create
End With

With Brick
    .Reset
    .Name "substrate"
    .Component "component1"
    .Material "Rogers4350B"
    .Xrange "-4", "16"
    .Yrange "-6", "6"
    .Zrange "-0.762", "0"
    .Create
End With

With Brick
    .Reset
    .Name "ground"
    .Component "component1"
    .Material "PEC"
    .Xrange "-4", "16"
    .Yrange "-6", "6"
    .Zrange "-0.797", "-0.762"
    .Create
End With

With Brick
    .Reset
    .Name "thru_main"
    .Component "feed"
    .Material "PEC"
    .Xrange "-2", "14"
    .Yrange "1", "2.6"
    .Zrange "0", "0.035"
    .Create
End With

With Brick
    .Reset
    .Name "thru_stub_-2.0"
    .Component "feed"
    .Material "PEC"
    .Xrange "-2", "-0.4"
    .Yrange "1", "4.5"
    .Zrange "0", "0.035"
    .Create
End With

With Brick
    .Reset
    .Name "thru_stub_12.4"
    .Component "feed"
    .Material "PEC"
    .Xrange "12.4", "14"
    .Yrange "1", "4.5"
    .Zrange "0", "0.035"
    .Create
End With

With Brick
    .Reset
    .Name "arm_feed_-2.0"
    .Component "feed"
    .Material "PEC"
    .Xrange "-2", "0"
    .Yrange "-1.6", "0"
    .Zrange "0", "0.035"
    .Create
End With

With Brick
    .Reset
    .Name "arm_feed_12.0"
    .Component "feed"
    .Material "PEC"
    .Xrange "12", "14"
    .Yrange "-1.6", "0"
    .Zrange "0", "0.035"
    .Create
End With

With Brick
    .Reset
    .Name "arm_stub_-2.0"
    .Component "feed"
    .Material "PEC"
    .Xrange "-2", "-0.4"
    .Yrange "-4", "0"
    .Zrange "0", "0.035"
    .Create
End With

With Brick
    .Reset
    .Name "arm_stub_12.4"
    .Component "feed"
    .Material "PEC"
    .Xrange "12.4", "14"
    .Yrange "-4", "0"
    .Zrange "0", "0.035"
    .Create
End With

With Polygon
    .Reset
    .Name "arm_init_curve"
    .Curve "arm_init_curve"
    .Point "0", "-1.6"
    .LineTo "12", "-1.6"
    .LineTo "12", "0"
    .LineTo "0", "0"
    .Create
End With

With Extrude
    .Reset
    .Name "arm_init"
    .Component "design_region"
    .Material "PEC"
    .Origin "0.0", "0.0", "0.0"
    .PlaneNormal "0", "0", "1"
    .Height "0.035"
    .Twist "0"
    .Taper "0"
    .Create
End With


With Port
    .Reset
    .PortNumber "1"
    .Label "p1"
    .Folder ""
    .NumberOfModes "1"
    .AdjustPolarization "False"
    .PolarizationAngle "0.0"
    .ReferencePlaneDistance "0"
    .TextSize "50"
    .TextMaxLimit "1"
    .Coordinates "Ranges"
    .Orientation "negative"
    .PortOnBound "False"
    .ClipPickedPortToBound "False"
    .Xrange "-3.6", "1.2"
    .Yrange "4.5", "4.5"
    .Zrange "-0.762", "2"
    .Create
End With

With Port
    .Reset
    .PortNumber "2"
    .Label "p2"
    .Folder ""
    .NumberOfModes "1"
    .AdjustPolarization "False"
    .PolarizationAngle "0.0"
    .ReferencePlaneDistance "0"
    .TextSize "50"
    .TextMaxLimit "1"
    .Coordinates "Ranges"
    .Orientation "negative"
    .PortOnBound "False"
    .ClipPickedPortToBound "False"
    .Xrange "10.8", "15.6"
    .Yrange "4.5", "4.5"
    .Zrange "-0.762", "2"
    .Create
End With

With Port
    .Reset
    .PortNumber "3"
    .Label "p3"
    .Folder ""
    .NumberOfModes "1"
    .AdjustPolarization "False"
    .PolarizationAngle "0.0"
    .ReferencePlaneDistance "0"
    .TextSize "50"
    .TextMaxLimit "1"
    .Coordinates "Ranges"
    .Orientation "positive"
    .PortOnBound "False"
    .ClipPickedPortToBound "False"
    .Xrange "-3.6", "1.2"
    .Yrange "-4", "-4"
    .Zrange "-0.762", "2"
    .Create
End With

With Port
    .Reset
    .PortNumber "4"
    .Label "p4"
    .Folder ""
    .NumberOfModes "1"
    .AdjustPolarization "False"
    .PolarizationAngle "0.0"
    .ReferencePlaneDistance "0"
    .TextSize "50"
    .TextMaxLimit "1"
    .Coordinates "Ranges"
    .Orientation "positive"
    .PortOnBound "False"
    .ClipPickedPortToBound "False"
    .Xrange "10.8", "15.6"
    .Yrange "-4", "-4"
    .Zrange "-0.762", "2"
    .Create
End With

With Excitation
    .Reset
    .Name "excitation1"
    .Port portnum
    .ModeIndex "1"
    .Create
End With

With Monitor
    .Reset
    .Name "e5"
    .Dimension "Volume"
    .Domain "Frequency"
    .FieldType "Efield"
    .MonitorValue "5.0"
    .UseSubvolume "False"
    .Create
End With

With Monitor
    .Reset
    .Name "h5"
    .Dimension "Volume"
    .Domain "Frequency"
    .FieldType "Hfield"
    .MonitorValue "5.0"
    .UseSubvolume "False"
    .Create
End With

With Boundary
    .Xmin "magnetic"
    .Xmax "magnetic"
    .Ymin "magnetic"
    .Ymax "magnetic"
    .Zmin "magnetic"
    .Zmax "electric"
End With

With Solver
    .Method "Hexahedral"
    .CalculationType "TD-S"
    .StimulationPort "All"
    .StimulationMode "All"
    .SteadyStateLimit "-30"
    .MeshAdaption "False"
    .AutoNormImpedance "False"
    .NormingImpedance "50"
End With

    SaveAs fname
End Sub